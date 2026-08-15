#!/usr/bin/env python3
"""Installed one-shot, offline-only Okto Pulse board-KG recovery executor.

This program deliberately does *not* enter the Community ASGI lifespan.  It
composes the installed providers, validates the already-migrated relational
schema, applies persisted settings, and directly drives one
exact ``ConsolidationClaimScope``.  It never starts an HTTP/MCP listener, a
worker registry, a scheduler, stale-claim recovery, or DLQ auto-drain.

Execution is intentionally cumbersome and fail-closed:

* ``--data-home`` and ``--board-id`` are always explicit;
* execution (live or rehearsal-copy) requires the exact install fingerprint;
* a live Community serve lock and heartbeat cover the complete operation;
* the rebuild service receives a nominal, revocable recovery capability only
  after the offline proofs have passed;
* source-set, DLQ and non-target queue fingerprints are continuously checked;
* every terminal report/promotion/checkpoint/lock/graph invariant must pass.
* live execution requires a path-bound, short-lived, single-use attestation
  produced by a successful run against an exact copy of that same data-home.

Use ``--inspect-install`` (still with explicit data-home and board id) to print
the fingerprint that a separately reviewed invocation can pin.  That mode does
not import Pulse or touch the supplied data-home.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import sysconfig
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import UUID


CORE_DISTRIBUTION = "okto-pulse-core"
COMMUNITY_DISTRIBUTION = "okto-pulse"
EXPECTED_VERSION = "0.3.2"
LADYBUG_DISTRIBUTION = "ladybug"
EXPECTED_LADYBUG_VERSION = "0.16.1"
KUZU_DISTRIBUTION = "kuzu"
EXPECTED_KUZU_VERSION = "0.11.3"
SQLALCHEMY_DISTRIBUTION = "SQLAlchemy"
EXPECTED_SQLALCHEMY_VERSION = "2.0.51"
AIOSQLITE_DISTRIBUTION = "aiosqlite"
EXPECTED_AIOSQLITE_VERSION = "0.22.1"
RECOVERY_ENTRYPOINT = (
    "okto-pulse-kg-recovery-only = okto_pulse.community.kg_recovery_only:main"
)
RECOVERY_LAUNCHER_NAMES = frozenset(
    {
        "okto-pulse-kg-recovery-only",
        "okto-pulse-kg-recovery-only.exe",
        "okto-pulse-kg-recovery-only-script.py",
    }
)
REHEARSAL_RECEIPT_SCHEMA = "pulse_kg_recovery_rehearsal.v1"
REHEARSAL_RECEIPT_TTL_SECONDS = 7_200
REHEARSAL_RECEIPT_MAX_BYTES = 65_536
DEFAULT_OFFLINE_PORTS = (8100, 8101)
OPERATION = "rebuild"
CognitiveLedgerRecord = tuple[str, Mapping[str, Any]]
MAX_COGNITIVE_BASELINE_RECORDS = 4_096
MAX_COGNITIVE_BASELINE_RECORD_BYTES = 16 * 1024 * 1024
MAX_GOVERNED_REBUILD_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_LEGACY_PROTECTED_QUEUE_ROWS = 16_384
MAX_LEGACY_PROTECTED_QUEUE_BYTES = 32 * 1024 * 1024
MAX_RECOVERY_SQLITE_TABLES = 512
MAX_RECOVERY_SQLITE_SCHEMA_OBJECTS = 4_096
CHECKPOINT_STATE_DRAINING = "draining"
CHECKPOINT_STATE_COMPLETED = "completed"
POST_DRAIN_CHECKPOINT_STATES = frozenset({"restored", "promoted", "completed"})
LEGACY_QUEUE_ONLY_OUTCOME_CODE = "legacy_manual_restore_queue_only_reconciled"
LEGACY_QUEUE_ONLY_CHECKPOINT_STATES = frozenset(
    {"blocked", "compensating", "compensation_failed", "failed"}
)
EXPECTED_SQLITE_USER_VERSION = 0
REQUIRED_RECOVERY_COLUMNS: Mapping[str, frozenset[str]] = {
    "boards": frozenset({"id", "owner_id", "settings"}),
    "app_settings": frozenset({"key", "value"}),
    "consolidation_queue": frozenset(
        {
            "id",
            "board_id",
            "artifact_type",
            "artifact_id",
            "work_kind",
            "generation",
            "payload",
            "delete_event_id",
            "priority",
            "source",
            "status",
            "triggered_at",
            "claimed_by_session_id",
            "claim_token",
            "claimed_at",
            "last_error",
            "worker_id",
            "claim_timeout_at",
            "attempts",
            "next_retry_at",
        }
    ),
    "consolidation_dead_letter": frozenset(
        {
            "id",
            "board_id",
            "artifact_type",
            "artifact_id",
            "original_queue_id",
            "attempts",
            "errors",
            "dead_lettered_at",
        }
    ),
}
EXPECTED_APP_SETTINGS_TABLE_INFO = (
    ("key", "VARCHAR(64)", 1, None, 1),
    ("value", "VARCHAR(64)", 1, None, 0),
)


class RecoveryRefused(RuntimeError):
    """A fail-closed proof or terminal gate refused the recovery."""


@dataclass(frozen=True, slots=True)
class DistributionEvidence:
    name: str
    version: str
    fingerprint: str
    file_count: int
    editable: bool


@dataclass(frozen=True, slots=True)
class InstallEvidence:
    fingerprint: str
    distributions: tuple[DistributionEvidence, ...]
    runtime: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    fingerprint: str


@dataclass(slots=True)
class ServiceBundle:
    artifact_store: Any
    confirmation_store: Any
    generation_repository: Any
    manifest_store: Any
    orphan_scanner: Any
    operation_reservation: Any
    service: Any
    single_writer_lock: Any
    source_enumerator: Any
    event_manifest_bindings: dict[str, EventManifestBinding]


@dataclass(frozen=True, slots=True)
class EventManifestBinding:
    """In-process authority for the terminal event's cognitive projection."""

    board_id: str
    preflight_hash: str
    rebaseline_evidence_id: str | None = None
    rebaseline_target_source_set_hash: str | None = None
    rebaseline_materializable_sources: tuple[Mapping[str, Any], ...] | None = None


@dataclass(frozen=True, slots=True)
class RecoveryRunPlan:
    mode: str
    manifest: Any
    source_set: Any | None
    preflight_hash: str
    run_id: str | None = None
    confirmation_ref: str | None = None
    user_reason: str | None = None
    receipt: Mapping[str, Any] | None = None
    terminal_audit: Mapping[str, Any] | None = None
    compensated_queue_adoption: CompensatedQueueAdoption | None = None
    rebaseline_evidence: Mapping[str, Any] | None = None
    manifest_verification_failure: str | None = None
    frozen_terminal: bool = False
    previous_terminal_receipt: Mapping[str, Any] | None = None
    previous_terminal_audit: Mapping[str, Any] | None = None
    legacy_queue_only: LegacyQueueOnlyReconciliation | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedManifestBinding:
    """Minimal receipt-bound identity used only to drive Core compensation."""

    board_id: str
    manifest_ref: str
    source_set_hash: str


@dataclass(frozen=True, slots=True)
class CompensatedQueueAdoption:
    """Durable authority to retag exact tombstones left by a prior compensation."""

    source: str
    manifest_ref: str
    identities: frozenset[tuple[str, str]]
    membership_by_identity: Mapping[tuple[str, str], Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class LegacyQueueOnlyReconciliation:
    """Exact authority for the manually-restored legacy F06 queue lane."""

    intent: Any
    command: Any
    intent_receipt: Any
    checkpoint_relative: str
    checkpoint_baseline: Mapping[str, Any]
    terminal: bool
    adoption: CompensatedQueueAdoption | None = None


@dataclass(frozen=True, slots=True)
class CognitiveLedgerBaseline:
    """Immutable pre-service view used to prove exact cognitive carry-forward."""

    records: tuple[CognitiveLedgerRecord, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(event: str, **details: Any) -> None:
    payload = {"at": _utc_now(), "event": event, **details}
    print(json.dumps(payload, sort_keys=True, default=str), flush=True)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecoveryRefused("non_canonical_json_value") from exc


def _canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require(condition: bool, code: str, detail: str | None = None) -> None:
    if condition:
        return
    suffix = f": {detail}" if detail else ""
    raise RecoveryRefused(f"{code}{suffix}")


def _parse_board_id(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError("board-id must be a UUID") from exc
    canonical = str(parsed)
    if candidate.lower() != canonical:
        raise argparse.ArgumentTypeError("board-id must use canonical UUID form")
    return canonical


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one governed Pulse KG rebuild with no runtime workers.",
    )
    parser.add_argument(
        "--data-home",
        required=True,
        help="Explicit absolute Pulse data-home (never inferred).",
    )
    parser.add_argument("--board-id", required=True, type=_parse_board_id)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--inspect-install",
        action="store_true",
        help="Print installed-package evidence only; no Pulse import or I/O.",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Execute live only with a verified, single-use rehearsal receipt.",
    )
    mode.add_argument(
        "--rehearsal-copy-of",
        metavar="SOURCE_DATA_HOME",
        help=(
            "Execute only against --data-home after proving it is an exact "
            "recovery-critical copy of this separate, explicit source home."
        ),
    )
    parser.add_argument(
        "--expected-install-fingerprint",
        help=(
            "Required with --execute and --rehearsal-copy-of; exact digest "
            "printed by --inspect-install."
        ),
    )
    parser.add_argument(
        "--rehearsal-receipt",
        help=(
            "Absolute, path-bound successful rehearsal attestation required "
            "by --execute; it is consumed before live mutation."
        ),
    )
    parser.add_argument(
        "--rehearsal-receipt-out",
        help="New absolute attestation path required by --rehearsal-copy-of.",
    )
    parser.add_argument("--expected-core-version", default=EXPECTED_VERSION)
    parser.add_argument("--expected-community-version", default=EXPECTED_VERSION)
    parser.add_argument(
        "--expected-ladybug-version",
        default=EXPECTED_LADYBUG_VERSION,
    )
    parser.add_argument("--expected-kuzu-version", default=EXPECTED_KUZU_VERSION)
    parser.add_argument(
        "--expected-sqlalchemy-version",
        default=EXPECTED_SQLALCHEMY_VERSION,
    )
    parser.add_argument(
        "--expected-aiosqlite-version",
        default=EXPECTED_AIOSQLITE_VERSION,
    )
    parser.add_argument(
        "--reason",
        default="offline one-shot KG recovery after governed source verification",
    )
    parser.add_argument(
        "--offline-port",
        action="append",
        type=int,
        dest="offline_ports",
        help="Additional TCP port that must have no loopback listener.",
    )
    parser.add_argument("--admission-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--poll-seconds", type=float, default=0.10)
    parser.add_argument("--batch-size", type=int, default=5)
    args = parser.parse_args(argv)
    if not args.inspect_install and not args.expected_install_fingerprint:
        parser.error(
            "--execute/--rehearsal-copy-of requires --expected-install-fingerprint"
        )
    if args.execute and not args.rehearsal_receipt:
        parser.error("--execute requires --rehearsal-receipt")
    if args.execute and args.rehearsal_receipt_out:
        parser.error("--rehearsal-receipt-out is only valid for rehearsal")
    if args.rehearsal_copy_of and not args.rehearsal_receipt_out:
        parser.error("--rehearsal-copy-of requires --rehearsal-receipt-out")
    if args.rehearsal_copy_of and args.rehearsal_receipt:
        parser.error("--rehearsal-receipt is only valid with --execute")
    if args.inspect_install and (args.rehearsal_receipt or args.rehearsal_receipt_out):
        parser.error("rehearsal receipt options are invalid with --inspect-install")
    if args.admission_timeout_seconds <= 0 or args.run_timeout_seconds <= 0:
        parser.error("timeouts must be positive")
    if not (0.02 <= args.poll_seconds <= 5.0):
        parser.error("--poll-seconds must be between 0.02 and 5")
    if not (1 <= args.batch_size <= 50):
        parser.error("--batch-size must be between 1 and 50")
    if not args.reason.strip() or len(args.reason) > 2048 or "\x00" in args.reason:
        parser.error(
            "--reason must be non-empty, at most 2048 characters and contain no NUL"
        )
    ports = set(DEFAULT_OFFLINE_PORTS)
    for port in args.offline_ports or ():
        if not 1 <= port <= 65535:
            parser.error("--offline-port must be between 1 and 65535")
        ports.add(port)
    args.offline_ports = tuple(sorted(ports))
    return args


def _recovery_execution_contract(args: argparse.Namespace) -> dict[str, Any]:
    """Canonical non-secret invocation settings that rehearsal authorizes."""

    return {
        "operation": OPERATION,
        "reason": str(args.reason),
        "offline_ports": list(args.offline_ports),
        "admission_timeout_seconds": float(args.admission_timeout_seconds),
        "run_timeout_seconds": float(args.run_timeout_seconds),
        "poll_seconds": float(args.poll_seconds),
        "batch_size": int(args.batch_size),
    }


def _distribution_evidence(
    name: str,
    expected_version: str,
    *,
    include_python: bool,
    require_native: bool = False,
) -> DistributionEvidence:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise RecoveryRefused(f"installed_distribution_missing:{name}") from exc
    version = str(dist.version)
    _require(
        version == expected_version,
        "installed_distribution_version_mismatch",
        f"{name} expected={expected_version} actual={version}",
    )
    direct_url_text = dist.read_text("direct_url.json")
    editable = False
    if direct_url_text:
        try:
            direct_url = json.loads(direct_url_text)
        except json.JSONDecodeError as exc:
            raise RecoveryRefused(f"installed_direct_url_invalid:{name}") from exc
        editable = bool(dict(direct_url.get("dir_info") or {}).get("editable"))
    _require(not editable, "editable_install_refused", name)

    digest = hashlib.sha256()
    digest.update(f"{name}\0{version}\0".encode())
    count = 0
    native_count = 0
    for relative in sorted(dist.files or (), key=lambda item: str(item).lower()):
        relative_text = str(relative).replace("\\", "/")
        lowered = relative_text.lower()
        is_metadata = lowered.endswith(
            (
                ".dist-info/metadata",
                ".dist-info/record",
                ".dist-info/entry_points.txt",
            )
        )
        is_native = lowered.endswith((".pyd", ".dll", ".so", ".dylib"))
        is_python = include_python and lowered.endswith((".py", ".pyi"))
        basename = Path(relative_text).name.lower()
        is_recovery_launcher = name == COMMUNITY_DISTRIBUTION and basename in {
            "okto-pulse-kg-recovery-only",
            "okto-pulse-kg-recovery-only.exe",
            "okto-pulse-kg-recovery-only-script.py",
        }
        if not (is_metadata or is_native or is_python or is_recovery_launcher):
            continue
        absolute = Path(dist.locate_file(relative)).resolve()
        _require(
            absolute.is_file(), "installed_distribution_file_missing", str(absolute)
        )
        file_hash = hashlib.sha256(absolute.read_bytes()).hexdigest()
        digest.update(f"{relative_text}\0{file_hash}\0".encode())
        count += 1
        native_count += int(is_native)
    _require(count > 0, "installed_distribution_empty", name)
    _require(
        not require_native or native_count > 0,
        "installed_native_binary_missing",
        name,
    )
    return DistributionEvidence(
        name=name,
        version=version,
        fingerprint=digest.hexdigest(),
        file_count=count,
        editable=editable,
    )


def _install_evidence(
    *,
    core_version: str,
    community_version: str,
    ladybug_version: str,
    kuzu_version: str,
    sqlalchemy_version: str,
    aiosqlite_version: str,
) -> InstallEvidence:
    distributions = (
        _distribution_evidence(
            CORE_DISTRIBUTION,
            core_version,
            include_python=True,
        ),
        _distribution_evidence(
            COMMUNITY_DISTRIBUTION,
            community_version,
            include_python=True,
        ),
        _distribution_evidence(
            LADYBUG_DISTRIBUTION,
            ladybug_version,
            include_python=True,
            require_native=True,
        ),
        _distribution_evidence(
            KUZU_DISTRIBUTION,
            kuzu_version,
            include_python=True,
        ),
        _distribution_evidence(
            SQLALCHEMY_DISTRIBUTION,
            sqlalchemy_version,
            include_python=True,
        ),
        _distribution_evidence(
            AIOSQLITE_DISTRIBUTION,
            aiosqlite_version,
            include_python=True,
        ),
    )
    runtime = tuple(
        sorted(
            {
                "architecture": platform.architecture()[0],
                "cache_tag": str(sys.implementation.cache_tag),
                "machine": platform.machine(),
                "platform": sysconfig.get_platform(),
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
                "system": platform.system(),
            }.items()
        )
    )
    digest = hashlib.sha256()
    digest.update(
        json.dumps(dict(runtime), sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    for item in distributions:
        digest.update(f"{item.name}\0{item.version}\0{item.fingerprint}\0".encode())
    return InstallEvidence(digest.hexdigest(), distributions, runtime)


def _hash_executor_file() -> str:
    executor_path = Path(__file__)
    _require(not _is_filesystem_alias(executor_path), "recovery_executor_alias_refused")
    resolved = executor_path.resolve(strict=True)
    _require(resolved.is_file(), "recovery_executor_file_missing", str(resolved))
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def _hash_recovery_entrypoint_metadata() -> str:
    """Bind the attestation to its declaration and installed launcher bytes."""

    try:
        dist = metadata.distribution(COMMUNITY_DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise RecoveryRefused(
            f"installed_distribution_missing:{COMMUNITY_DISTRIBUTION}"
        ) from exc
    matching = tuple(
        entry
        for entry in dist.entry_points
        if entry.group == "console_scripts"
        and entry.name == "okto-pulse-kg-recovery-only"
    )
    _require(
        len(matching) == 1
        and matching[0].value == "okto_pulse.community.kg_recovery_only:main",
        "recovery_entrypoint_contract_missing",
        RECOVERY_ENTRYPOINT,
    )
    metadata_candidates = tuple(
        relative
        for relative in (dist.files or ())
        if str(relative)
        .replace("\\", "/")
        .lower()
        .endswith(".dist-info/entry_points.txt")
    )
    _require(
        len(metadata_candidates) == 1,
        "recovery_entrypoint_metadata_ambiguous",
        repr(tuple(str(item) for item in metadata_candidates)),
    )
    launcher_candidates = tuple(
        relative
        for relative in (dist.files or ())
        if Path(str(relative).replace("\\", "/")).name.lower()
        in RECOVERY_LAUNCHER_NAMES
    )
    _require(
        bool(launcher_candidates),
        "recovery_entrypoint_launcher_missing",
    )
    digest = hashlib.sha256()
    for relative in (*metadata_candidates, *launcher_candidates):
        path = Path(dist.locate_file(relative))
        _require(
            not _is_filesystem_alias(path),
            "recovery_entrypoint_file_alias_refused",
        )
        resolved = path.resolve(strict=True)
        _require(resolved.is_file(), "recovery_entrypoint_file_missing", str(resolved))
        relative_text = str(relative).replace("\\", "/")
        digest.update(f"{relative_text}\0".encode("utf-8"))
        digest.update(hashlib.sha256(resolved.read_bytes()).digest())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _installed_recovery_launcher_paths() -> frozenset[Path]:
    """Resolve the exact installed console launchers eligible as self-parent."""

    try:
        dist = metadata.distribution(COMMUNITY_DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise RecoveryRefused(
            f"installed_distribution_missing:{COMMUNITY_DISTRIBUTION}"
        ) from exc
    candidates = tuple(
        relative
        for relative in (dist.files or ())
        if Path(str(relative).replace("\\", "/")).name.lower()
        in RECOVERY_LAUNCHER_NAMES
    )
    _require(bool(candidates), "recovery_entrypoint_launcher_missing")
    resolved: set[Path] = set()
    for relative in candidates:
        path = Path(dist.locate_file(relative))
        _assert_no_symlink_components(
            path,
            "recovery_entrypoint_file_alias_refused",
        )
        _require(
            not _is_filesystem_alias(path),
            "recovery_entrypoint_file_alias_refused",
        )
        launcher = path.resolve(strict=True)
        _require(
            launcher.is_file(),
            "recovery_entrypoint_file_missing",
            str(launcher),
        )
        resolved.add(launcher)
    return frozenset(resolved)


def _resolve_explicit_data_home(raw_value: str, *, require_existing: bool) -> Path:
    raw = Path(raw_value).expanduser()
    _require(raw.is_absolute(), "data_home_must_be_absolute", raw_value)
    resolved = raw.resolve(strict=require_existing)
    if require_existing:
        _require(resolved.is_dir(), "data_home_not_directory", str(resolved))
        _assert_no_symlink_components(raw, "data_home_alias_refused")
    return resolved


def _rehearsal_scope_snapshot(data_home: Path, board_id: str) -> dict[str, str]:
    """Hash recovery-critical state without following filesystem aliases."""

    snapshot: dict[str, str] = {}
    for relative_root in (
        Path("data"),
        Path("rebuild"),
        Path("quarantine"),
        Path("boards") / board_id,
    ):
        root = data_home / relative_root
        for relative, digest in _snapshot_tree_hashes(root).items():
            snapshot[(relative_root / relative).as_posix()] = digest
    _require(
        "data/pulse.db" in snapshot,
        "rehearsal_pulse_database_missing",
        str(data_home / "data" / "pulse.db"),
    )
    return snapshot


def _opened_file_identity(handle: Any, *, path: Path) -> tuple[int, int, int]:
    """Return a stable physical-file identity from an already-open handle.

    Hash equality cannot distinguish a byte-for-byte copy from a hardlink.  On
    Windows, ``st_ino`` has varied across Python/filesystem combinations, so
    use the authoritative volume serial + file index exposed by the kernel.
    POSIX ``st_dev`` + ``st_ino`` is the corresponding identity.  The link
    count is returned so a rehearsal target that aliases any third path is
    refused even when that third path is outside the recovery snapshot.
    """

    opened = os.fstat(handle.fileno())
    _require(
        stat.S_ISREG(opened.st_mode),
        "rehearsal_copy_identity_not_regular",
        str(path),
    )
    if os.name != "nt":
        device = int(opened.st_dev)
        inode = int(opened.st_ino)
        links = int(opened.st_nlink)
        _require(
            device >= 0 and inode > 0 and links >= 1,
            "rehearsal_copy_identity_unprovable",
            str(path),
        )
        return device, inode, links

    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = (
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            )

        get_information = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).GetFileInformationByHandle
        get_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        )
        get_information.restype = wintypes.BOOL
        information = _ByHandleFileInformation()
        native_handle = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
        succeeded = bool(get_information(native_handle, ctypes.byref(information)))
        error_code = int(ctypes.get_last_error()) if not succeeded else 0
    except Exception as exc:
        raise RecoveryRefused(f"rehearsal_copy_identity_unprovable:{path}") from exc
    _require(
        succeeded,
        "rehearsal_copy_identity_unprovable",
        f"{path}:winerror={error_code}",
    )
    volume_serial = int(information.dwVolumeSerialNumber)
    file_index = (int(information.nFileIndexHigh) << 32) | int(
        information.nFileIndexLow
    )
    links = int(information.nNumberOfLinks)
    _require(
        volume_serial > 0 and file_index > 0 and links >= 1,
        "rehearsal_copy_identity_unprovable",
        str(path),
    )
    return volume_serial, file_index, links


def _stable_file_identity(path: Path) -> tuple[int, int, int]:
    """Read one physical identity while proving the path did not change."""

    _assert_no_symlink_components(path, "rehearsal_copy_alias_refused")
    try:
        before = path.stat(follow_symlinks=False)
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            identity = _opened_file_identity(handle, path=path)
            opened_after = os.fstat(handle.fileno())
        after = path.stat(follow_symlinks=False)
    except RecoveryRefused:
        raise
    except OSError as exc:
        raise RecoveryRefused(f"rehearsal_copy_identity_unprovable:{path}") from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    _require(
        all(
            getattr(before, field)
            == getattr(opened_before, field)
            == getattr(opened_after, field)
            == getattr(after, field)
            for field in stable_fields
        ),
        "rehearsal_copy_identity_changed",
        str(path),
    )
    return identity


def _assert_rehearsal_files_independent(
    *,
    source_home: Path,
    target_home: Path,
    relative_paths: Iterable[str],
) -> None:
    """Prove the recovery copy contains independent physical file objects."""

    source_identities: dict[tuple[int, int], str] = {}
    target_identities: dict[tuple[int, int], str] = {}
    for relative in sorted(relative_paths):
        source_path = source_home / Path(relative)
        target_path = target_home / Path(relative)
        source_device, source_inode, _source_links = _stable_file_identity(source_path)
        target_device, target_inode, target_links = _stable_file_identity(target_path)
        source_identity = (source_device, source_inode)
        target_identity = (target_device, target_inode)
        _require(
            target_links == 1,
            "rehearsal_copy_alias_refused",
            f"target_hardlink:{relative}:links={target_links}",
        )
        _require(
            source_identity not in source_identities,
            "rehearsal_source_alias_refused",
            f"{source_identities.get(source_identity)}:{relative}",
        )
        _require(
            target_identity not in target_identities,
            "rehearsal_copy_alias_refused",
            f"target_internal_hardlink:{target_identities.get(target_identity)}:{relative}",
        )
        source_identities[source_identity] = relative
        target_identities[target_identity] = relative
    shared = set(source_identities).intersection(target_identities)
    _require(
        not shared,
        "rehearsal_copy_alias_refused",
        repr(
            sorted(
                (
                    source_identities[identity],
                    target_identities[identity],
                )
                for identity in shared
            )
        ),
    )


def _assert_rehearsal_copy(
    *,
    source_home: Path,
    target_home: Path,
    board_id: str,
) -> tuple[dict[str, str], str]:
    _require(source_home != target_home, "rehearsal_source_equals_target")
    _require(
        source_home not in target_home.parents
        and target_home not in source_home.parents,
        "rehearsal_source_target_nested",
    )
    source_snapshot = _rehearsal_scope_snapshot(source_home, board_id)
    target_snapshot = _rehearsal_scope_snapshot(target_home, board_id)
    _require(
        target_snapshot == source_snapshot,
        "rehearsal_copy_fingerprint_mismatch",
    )
    _assert_rehearsal_files_independent(
        source_home=source_home,
        target_home=target_home,
        relative_paths=source_snapshot,
    )
    source_schema = _sqlite_schema_fingerprint(source_home / "data" / "pulse.db")
    target_schema = _sqlite_schema_fingerprint(target_home / "data" / "pulse.db")
    _require(source_schema == target_schema, "rehearsal_copy_schema_mismatch")
    return source_snapshot, source_schema


def _configure_explicit_environment(data_home: Path) -> Path:
    db_path = (data_home / "data" / "pulse.db").resolve()
    kg_base = data_home.resolve()
    values = {
        "DATA_DIR": str(data_home),
        "OKTO_PULSE_HOME": str(data_home),
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}",
        "KG_BASE_DIR": str(kg_base),
        "UPLOAD_DIR": str((data_home / "uploads").resolve()),
        "METRICS_DIR": str((data_home / "metrics").resolve()),
        # Defence in depth.  These flags do not replace the fact that no
        # lifespan/registry/scheduler is ever started.
        "KG_DAILY_TICK_DISABLED": "1",
    }
    for key, value in values.items():
        os.environ[key] = value
    return db_path


def _port_is_listening(port: int) -> bool:
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.15)
            try:
                if probe.connect_ex((host, port)) == 0:
                    return True
            except OSError:
                continue
    return False


def _assert_ports_offline(ports: Iterable[int]) -> None:
    active = [port for port in ports if _port_is_listening(port)]
    _require(not active, "offline_listener_detected", repr(active))


def _is_pulse_runtime_process(name: str, command_line: str) -> bool:
    """Recognize a Pulse runtime without treating an ancestor shell as one."""

    executable = Path(name.strip().lower()).name.removesuffix(".exe")
    if not (
        executable.startswith("python")
        or executable in {"okto-pulse", "okto-pulse-kg-recovery-only", "uvicorn"}
    ):
        return False
    # Dedicated Pulse launchers are authoritative process identities even
    # when CIM/procfs cannot expose their command line (for example an
    # elevated peer). Self-exemption below remains stricter and requires the
    # exact command/path/ancestry binding, so missing details fail closed.
    if executable in {"okto-pulse", "okto-pulse-kg-recovery-only"}:
        return True

    normalized = " ".join(
        command_line.lower()
        .replace("\\", "/")
        .replace('"', "")
        .replace("'", "")
        .split()
    )
    launcher_serve = re.search(
        r"(?:^|[/\s])okto-pulse(?:\.exe)?\s+serve(?:\s|$)", normalized
    )
    module_runtime = any(
        marker in normalized
        for marker in (
            "okto_pulse.community.main",
            "pulse-workerless-recovery-launch.py",
            "pulse-kg-recovery-only.py",
            "okto_pulse.community.kg_recovery_only",
            "okto-pulse-kg-recovery-only",
            "uvicorn okto_pulse",
        )
    )
    return launcher_serve is not None or module_runtime


def _windows_command_prefix(
    command_line: str,
    *,
    limit: int = 2,
) -> tuple[str, ...]:
    """Parse only the simple quoted executable prefix emitted by Win32 CIM."""

    raw = command_line
    tokens: list[str] = []
    offset = 0
    while len(tokens) < limit:
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset >= len(raw):
            break
        if raw[offset] in {'"', "'"}:
            quote = raw[offset]
            closing = raw.find(quote, offset + 1)
            if closing <= offset + 1:
                return ()
            tokens.append(raw[offset + 1 : closing])
            offset = closing + 1
            continue
        closing = offset
        while closing < len(raw) and not raw[closing].isspace():
            closing += 1
        tokens.append(raw[offset:closing])
        offset = closing
    return tuple(tokens)


def _same_process_image_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _windows_process_created_at(row: Mapping[str, Any]) -> datetime | None:
    raw = str(row.get("CreationDate") or "")
    legacy_match = re.fullmatch(r"/Date\((-?[0-9]+)(?:[+-][0-9]{4})?\)/", raw)
    if legacy_match is not None:
        try:
            return datetime.fromtimestamp(
                int(legacy_match.group(1)) / 1000,
                tz=timezone.utc,
            )
        except (OverflowError, OSError, ValueError):
            return None
    try:
        observed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        return None
    return observed.astimezone(timezone.utc)


def _resolved_process_path(value: Any) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve(strict=True)
    except OSError:
        return None


def _windows_exact_recovery_launcher_row(
    row: Mapping[str, Any],
    *,
    launcher_paths: frozenset[Path],
) -> Path | None:
    name = Path(str(row.get("Name") or "").strip().lower()).name
    command_line = str(row.get("CommandLine") or "")
    if name not in RECOVERY_LAUNCHER_NAMES or not _is_pulse_runtime_process(
        name, command_line
    ):
        return None
    executable = _resolved_process_path(row.get("ExecutablePath"))
    prefix = _windows_command_prefix(command_line, limit=1)
    if executable is None or len(prefix) != 1:
        return None
    if not any(
        _same_process_image_path(executable, launcher) for launcher in launcher_paths
    ):
        return None
    argv0 = _resolved_process_path(prefix[0])
    if argv0 is None or not _same_process_image_path(argv0, executable):
        return None
    return executable


def _windows_self_recovery_ancestor_pids(
    rows: Sequence[Mapping[str, Any]],
    *,
    current_pid: int,
    launcher_paths: frozenset[Path],
) -> frozenset[int]:
    """Allow only the exact launcher/shim ancestry that owns this Python."""

    by_pid: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            by_pid[pid] = row
    current = by_pid.get(current_pid)
    if current is None:
        return frozenset()
    current_created = _windows_process_created_at(current)
    if current_created is None:
        return frozenset()
    try:
        parent_pid = int(current.get("ParentProcessId") or 0)
    except (TypeError, ValueError):
        return frozenset()
    parent = by_pid.get(parent_pid)
    if parent is None:
        return frozenset()
    parent_created = _windows_process_created_at(parent)
    if parent_created is None or parent_created > current_created:
        return frozenset()

    # Some launchers directly host Python; the installed launcher is then the
    # immediate parent and no interpreter shim needs to be exempted.
    direct_launcher = _windows_exact_recovery_launcher_row(
        parent,
        launcher_paths=launcher_paths,
    )
    if direct_launcher is not None:
        return frozenset({parent_pid})

    # pip/distlib on Windows normally creates:
    # launcher.exe -> venv/Scripts/python.exe -> base python.exe (current).
    # Exempt the middle interpreter only when its own parent is the exact
    # installed launcher and both Python command lines name that launcher as
    # argv[1]. This does not exempt a nested ``python -m`` recovery process.
    parent_name = Path(str(parent.get("Name") or "").strip().lower()).name
    if not parent_name.startswith("python"):
        return frozenset()
    try:
        launcher_pid = int(parent.get("ParentProcessId") or 0)
    except (TypeError, ValueError):
        return frozenset()
    launcher_row = by_pid.get(launcher_pid)
    if launcher_row is None:
        return frozenset()
    launcher_created = _windows_process_created_at(launcher_row)
    launcher_path = _windows_exact_recovery_launcher_row(
        launcher_row,
        launcher_paths=launcher_paths,
    )
    if (
        launcher_created is None
        or launcher_created > parent_created
        or launcher_path is None
    ):
        return frozenset()
    parent_prefix = _windows_command_prefix(
        str(parent.get("CommandLine") or ""), limit=2
    )
    current_prefix = _windows_command_prefix(
        str(current.get("CommandLine") or ""), limit=2
    )
    if len(parent_prefix) != 2 or len(current_prefix) != 2:
        return frozenset()
    parent_interpreter = _resolved_process_path(parent.get("ExecutablePath"))
    parent_argv0 = _resolved_process_path(parent_prefix[0])
    parent_launcher_arg = _resolved_process_path(parent_prefix[1])
    current_launcher_arg = _resolved_process_path(current_prefix[1])
    if (
        parent_interpreter is None
        or parent_argv0 is None
        or parent_launcher_arg is None
        or current_launcher_arg is None
        or not _same_process_image_path(parent_interpreter, parent_argv0)
        or not _same_process_image_path(parent_launcher_arg, launcher_path)
        or not _same_process_image_path(current_launcher_arg, launcher_path)
    ):
        return frozenset()
    return frozenset({parent_pid, launcher_pid})


def _windows_pulse_process_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    current_pid: int,
    launcher_paths: frozenset[Path],
) -> tuple[dict[str, Any], ...]:
    self_ancestor_pids = _windows_self_recovery_ancestor_pids(
        rows,
        current_pid=current_pid,
        launcher_paths=launcher_paths,
    )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        command_line = str(row.get("CommandLine") or "").lower()
        name = str(row.get("Name") or "")
        if pid == current_pid or pid in self_ancestor_pids:
            continue
        if _is_pulse_runtime_process(name, command_line):
            candidates.append({"pid": pid, "name": name})
    return tuple(candidates)


def _pulse_process_candidates() -> tuple[dict[str, Any], ...]:
    """Return other likely Pulse server processes without exposing commands."""

    candidates: list[dict[str, Any]] = []
    current_pid = os.getpid()

    if os.name == "nt":
        command = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,CreationDate,Name,"
            "ExecutablePath,CommandLine | "
            "ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        _require(
            completed.returncode == 0,
            "offline_process_scan_failed",
            f"exit={completed.returncode}",
        )
        try:
            payload = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RecoveryRefused("offline_process_scan_invalid") from exc
        rows = payload if isinstance(payload, list) else [payload]
        valid_rows = tuple(row for row in rows if isinstance(row, Mapping))
        return _windows_pulse_process_candidates(
            valid_rows,
            current_pid=current_pid,
            launcher_paths=_installed_recovery_launcher_paths(),
        )

    proc_root = Path("/proc")
    _require(proc_root.is_dir(), "offline_process_scan_unavailable")
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == current_pid:
            continue
        try:
            command_line = (process_dir / "cmdline").read_bytes().replace(b"\0", b" ")
            normalized = command_line.decode(errors="replace").lower()
            process_name = (process_dir / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if _is_pulse_runtime_process(process_name, normalized):
            candidates.append({"pid": int(process_dir.name), "name": process_name})
    return tuple(candidates)


def _assert_no_pulse_processes() -> None:
    candidates = _pulse_process_candidates()
    _require(not candidates, "offline_pulse_process_detected", repr(candidates))


class OfflineProcessProbe:
    """Serialize/caches the expensive OS process oracle for lifetime checks."""

    def __init__(self, *, max_age_seconds: float = 1.0) -> None:
        self._max_age = max(0.1, float(max_age_seconds))
        self._lock = threading.Lock()
        self._observed_at = 0.0
        self._result = False

    def is_offline(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._observed_at <= self._max_age:
                return self._result
            try:
                self._result = not _pulse_process_candidates()
            except BaseException:
                self._result = False
            self._observed_at = time.monotonic()
            return self._result


def _sqlite_connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _normalize_sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _bounded_sqlite_snapshot_rows(
    cursor: sqlite3.Cursor,
    *,
    code: str,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> tuple[tuple[Any, ...], ...]:
    """Materialize one logical SQLite cut under explicit availability bounds."""

    row_limit = MAX_LEGACY_PROTECTED_QUEUE_ROWS if max_rows is None else max_rows
    byte_limit = MAX_LEGACY_PROTECTED_QUEUE_BYTES if max_bytes is None else max_bytes
    rows: list[tuple[Any, ...]] = []
    total_bytes = 0
    for raw in cursor:
        _require(len(rows) < row_limit, f"{code}_row_limit_exceeded")
        row = tuple(raw)
        encoded = _canonical_json_bytes(
            [_normalize_sqlite_value(value) for value in row]
        )
        total_bytes += len(encoded)
        _require(total_bytes <= byte_limit, f"{code}_byte_limit_exceeded")
        rows.append(row)
    return tuple(rows)


def _fingerprint_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    payload = {
        "columns": list(columns),
        "rows": [[_normalize_sqlite_value(value) for value in row] for row in rows],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sqlite_logical_fingerprints(
    db_path: Path,
    *,
    exclude_tables: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Fingerprint every user table in one read transaction, independent of WAL bytes."""

    result: dict[str, str] = {}
    with _sqlite_connect_readonly(db_path) as connection:
        connection.execute("BEGIN")
        table_name_rows = _bounded_sqlite_snapshot_rows(
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ),
            code="sqlite_logical_table_inventory",
            max_rows=MAX_RECOVERY_SQLITE_TABLES,
            max_bytes=MAX_LEGACY_PROTECTED_QUEUE_BYTES,
        )
        table_names = tuple(str(row[0]) for row in table_name_rows)
        for table_name in table_names:
            if table_name in exclude_tables:
                continue
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            columns = tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            )
            _require(bool(columns), "sqlite_logical_table_shape_missing", table_name)
            quoted_columns = ", ".join(
                '"' + column.replace('"', '""') + '"' for column in columns
            )
            rows = list(
                _bounded_sqlite_snapshot_rows(
                    connection.execute(f"SELECT {quoted_columns} FROM {quoted_table}"),
                    code=f"sqlite_logical_table_{table_name}",
                )
            )
            rows.sort(
                key=lambda row: _canonical_json_bytes(
                    [_normalize_sqlite_value(value) for value in row]
                )
            )
            result[table_name] = _fingerprint_rows(columns, rows)
        connection.rollback()
    return result


def _table_columns(db_path: Path, table: str) -> tuple[str, ...]:
    _require(table.replace("_", "").isalnum(), "unsafe_table_name", table)
    with _sqlite_connect_readonly(db_path) as connection:
        return tuple(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        )


def _table_info(
    db_path: Path,
    table: str,
) -> tuple[tuple[str, str, int, str | None, int], ...]:
    """Return the bounded SQLite column contract without importing the ORM."""

    _require(table.replace("_", "").isalnum(), "unsafe_table_name", table)
    with _sqlite_connect_readonly(db_path) as connection:
        return tuple(
            (
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
            )
            for row in connection.execute(f"PRAGMA table_info({table})")
        )


def _sqlite_schema_fingerprint(db_path: Path) -> str:
    """Fingerprint the complete existing SQLite DDL without changing it."""

    with _sqlite_connect_readonly(db_path) as connection:
        schema_rows = _bounded_sqlite_snapshot_rows(
            connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ),
            code="sqlite_schema_inventory",
            max_rows=MAX_RECOVERY_SQLITE_SCHEMA_OBJECTS,
            max_bytes=MAX_LEGACY_PROTECTED_QUEUE_BYTES,
        )
        pragmas = {
            "application_id": int(
                connection.execute("PRAGMA application_id").fetchone()[0]
            ),
            "schema_version": int(
                connection.execute("PRAGMA schema_version").fetchone()[0]
            ),
            "user_version": int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            ),
        }
    encoded = json.dumps(
        {"pragmas": pragmas, "sqlite_master": schema_rows},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sqlite_storage_fingerprints(db_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            _require(path.is_file(), "sqlite_storage_not_file", str(path))
            _require(
                not _is_filesystem_alias(path),
                "sqlite_storage_alias_refused",
                str(path),
            )
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    _require(db_path.name in result, "pulse_database_missing", str(db_path))
    return result


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_filesystem_alias(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return path.is_symlink()
    return _stat_is_filesystem_alias(details)


def _stat_is_filesystem_alias(details: os.stat_result) -> bool:
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    file_attributes = int(getattr(details, "st_file_attributes", 0))
    return stat.S_ISLNK(details.st_mode) or bool(
        reparse_flag and file_attributes & reparse_flag
    )


def _assert_no_symlink_components(path: Path, code: str) -> None:
    current = path
    while True:
        try:
            details = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RecoveryRefused(f"{code}_unverifiable:{current}") from exc
        else:
            _require(
                not current.is_symlink() and not _stat_is_filesystem_alias(details),
                code,
                str(current),
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _path_is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _assert_safe_json_filename(path: Path, code: str) -> None:
    _require(
        path.name.lower().endswith(".json")
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,149}", path.name) is not None,
        code,
        path.name,
    )


def _resolve_external_new_file(
    raw_value: str,
    *,
    protected_homes: Sequence[Path],
    purpose: str,
) -> Path:
    raw = Path(raw_value).expanduser()
    _require(raw.is_absolute(), f"{purpose}_path_must_be_absolute", raw_value)
    _assert_no_symlink_components(raw.parent, f"{purpose}_parent_symlink_refused")
    parent = raw.parent.resolve(strict=True)
    _require(parent.is_dir(), f"{purpose}_parent_not_directory", str(parent))
    candidate = parent / raw.name
    _require(bool(raw.name), f"{purpose}_filename_missing")
    _assert_safe_json_filename(candidate, f"{purpose}_filename_invalid")
    _require(
        not candidate.exists() and not candidate.is_symlink(),
        f"{purpose}_already_exists",
        str(candidate),
    )
    for home in protected_homes:
        _require(
            not _path_is_within(candidate, home.resolve()),
            f"{purpose}_must_be_outside_data_home",
            str(home),
        )
    return candidate


def _resolve_external_existing_file(
    raw_value: str,
    *,
    protected_homes: Sequence[Path],
    purpose: str,
) -> Path:
    raw = Path(raw_value).expanduser()
    _require(raw.is_absolute(), f"{purpose}_path_must_be_absolute", raw_value)
    _assert_no_symlink_components(raw, f"{purpose}_symlink_refused")
    resolved = raw.resolve(strict=True)
    _require(resolved.is_file(), f"{purpose}_not_file", str(resolved))
    _assert_safe_json_filename(resolved, f"{purpose}_filename_invalid")
    for home in protected_homes:
        _require(
            not _path_is_within(resolved, home.resolve()),
            f"{purpose}_must_be_outside_data_home",
            str(home),
        )
    return resolved


def _read_bounded_stable_file(
    path: Path,
    *,
    code: str,
    max_bytes: int = REHEARSAL_RECEIPT_MAX_BYTES,
) -> bytes:
    try:
        before = path.stat()
    except OSError as exc:
        raise RecoveryRefused(f"{code}_unreadable:{path}") from exc
    _require(stat.S_ISREG(before.st_mode), f"{code}_not_regular", str(path))
    _require(
        before.st_size <= max_bytes,
        f"{code}_too_large",
        str(before.st_size),
    )
    try:
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            content = handle.read(max_bytes + 1)
            opened_after = os.fstat(handle.fileno())
    except OSError as exc:
        raise RecoveryRefused(f"{code}_unreadable:{path}") from exc
    after = path.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    _require(
        all(
            getattr(before, field)
            == getattr(opened_before, field)
            == getattr(opened_after, field)
            == getattr(after, field)
            for field in stable_fields
        ),
        f"{code}_changed_while_reading",
    )
    _require(
        len(content) <= max_bytes,
        f"{code}_too_large",
    )
    return content


def _strict_json_mapping(content: bytes, *, code: str) -> dict[str, Any]:
    def _object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RecoveryRefused(f"{code}_duplicate_key:{key}")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except RecoveryRefused:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RecoveryRefused(f"{code}_invalid_json") from exc
    _require(isinstance(decoded, dict), f"{code}_not_object")
    return decoded


def _sync_directory_durable(directory: Path, *, code: str) -> None:
    """Durably flush a POSIX directory entry or refuse the boundary."""

    _require(os.name != "nt", f"{code}_windows_requires_write_through_move")
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, flags)
        os.fsync(descriptor)
    except OSError as exc:
        raise RecoveryRefused(f"{code}_directory_sync_failed:{directory}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _windows_move_new_write_through(
    source: Path,
    target: Path,
    *,
    code: str,
) -> None:
    """Atomically publish without replacement using Win32 write-through."""

    _require(os.name == "nt", f"{code}_windows_move_on_non_windows")
    try:
        import ctypes
        from ctypes import wintypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move_file.restype = wintypes.BOOL
        moved = bool(move_file(str(source), str(target), 0x00000008))
        error_code = int(ctypes.get_last_error()) if not moved else 0
    except BaseException as exc:
        raise RecoveryRefused(f"{code}_write_through_move_unavailable") from exc
    if not moved:
        if target.exists() or target.is_symlink():
            raise RecoveryRefused(f"{code}_already_exists:{target}")
        raise RecoveryRefused(f"{code}_write_through_move_failed:{error_code}")


def _publish_new_file_durable(
    temporary: Path,
    target: Path,
    *,
    code: str,
) -> None:
    """Publish a fully fsynced temp file as a durable, exclusive name."""

    if os.name == "nt":
        _windows_move_new_write_through(temporary, target, code=code)
        return
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise RecoveryRefused(f"{code}_already_exists:{target}") from exc
    except OSError as exc:
        raise RecoveryRefused(f"{code}_exclusive_publish_failed:{target}") from exc
    # The target link must be durable before the temporary link is removed.
    # A crash between these two syncs leaves both names; target existence then
    # still refuses a second publish/consume attempt.
    _sync_directory_durable(target.parent, code=code)
    try:
        temporary.unlink()
    except OSError as exc:
        raise RecoveryRefused(f"{code}_temporary_unlink_failed:{temporary}") from exc
    _sync_directory_durable(target.parent, code=code)


def _write_new_json_file(path: Path, payload: Mapping[str, Any], *, code: str) -> None:
    content = _canonical_json_bytes(payload) + b"\n"
    _require(
        len(content) <= REHEARSAL_RECEIPT_MAX_BYTES,
        f"{code}_too_large",
    )
    nonce = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
    temporary = path.with_name(
        f".{path.name}.pending-{os.getpid()}-{threading.get_ident()}-{nonce}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _publish_new_file_durable(temporary, path, code=code)
    except FileExistsError as exc:
        raise RecoveryRefused(f"{code}_already_exists:{path}") from exc
    except RecoveryRefused:
        raise
    except OSError as exc:
        raise RecoveryRefused(f"{code}_write_failed:{path}") from exc
    finally:
        if temporary.exists() and not _is_filesystem_alias(temporary):
            with suppress(OSError):
                temporary.unlink()
    _require(
        _read_bounded_stable_file(path, code=code) == content,
        f"{code}_published_content_mismatch",
    )


def _consume_file_durable(
    source: Path,
    target: Path,
    *,
    expected_content: bytes,
    code: str,
) -> None:
    """Consume one receipt name durably before any live data-home write."""

    _require(source.parent == target.parent, f"{code}_cross_directory_refused")
    _require(
        _read_bounded_stable_file(source, code=code) == expected_content,
        f"{code}_content_changed",
    )
    _require(
        not target.exists() and not target.is_symlink(),
        f"{code}_already_exists",
        str(target),
    )
    if os.name == "nt":
        _windows_move_new_write_through(source, target, code=code)
    else:
        try:
            os.link(source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise RecoveryRefused(f"{code}_already_exists:{target}") from exc
        except OSError as exc:
            raise RecoveryRefused(f"{code}_exclusive_claim_failed:{target}") from exc
        _sync_directory_durable(target.parent, code=code)
        try:
            source.unlink()
        except OSError as exc:
            raise RecoveryRefused(f"{code}_source_unlink_failed:{source}") from exc
        _sync_directory_durable(target.parent, code=code)
    _require(
        not source.exists()
        and not source.is_symlink()
        and _read_bounded_stable_file(target, code=code) == expected_content,
        f"{code}_durability_postcondition_failed",
    )


def _parse_utc_timestamp(value: Any, *, code: str) -> datetime:
    _require(isinstance(value, str) and bool(value), code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RecoveryRefused(code) from exc
    _require(parsed.tzinfo is not None, code)
    _require(parsed.utcoffset() == timedelta(0), code)
    return parsed.astimezone(timezone.utc)


def _validate_storage_hashes(value: Any, *, code: str) -> dict[str, str]:
    _require(isinstance(value, Mapping), code)
    normalized = {str(key): str(item) for key, item in value.items()}
    _require(
        "pulse.db" in normalized
        and set(normalized).issubset({"pulse.db", "pulse.db-wal", "pulse.db-shm"}),
        code,
    )
    _require(all(_is_sha256(item) for item in normalized.values()), code)
    return normalized


def _validate_board_storage_hashes(value: Any, *, code: str) -> dict[str, str]:
    _require(isinstance(value, Mapping), code)
    _require(
        all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ),
        code,
    )
    normalized = dict(value)
    _require(set(normalized) == {"graph.lbug"}, code)
    _require(_is_sha256(normalized["graph.lbug"]), code)
    return normalized


_REHEARSAL_TERMINAL_FIELDS = frozenset(
    {
        "run_id",
        "manifest_ref",
        "source_set_hash",
        "current_kg_generation_id",
        "report_ref",
        "report_id",
        "publishable_status",
        "promotion_outcome",
        "event_emitted",
        "orphan_count",
        "quarantine_id",
        "report_source_hash",
        "graph_schema_version",
        "sqlite_storage_before",
        "sqlite_storage_after",
        "board_storage_post_teardown",
        "board_storage_post_teardown_sha256",
        "rehearsal_source_unchanged",
        "rehearsal_source_home",
    }
)


def _validate_rehearsal_terminal_evidence(
    value: Any,
    *,
    source_home: Path,
    source_storage: Mapping[str, str],
) -> dict[str, Any]:
    _require(isinstance(value, Mapping), "rehearsal_terminal_evidence_invalid")
    evidence = dict(value)
    _require(
        set(evidence) == _REHEARSAL_TERMINAL_FIELDS,
        "rehearsal_terminal_evidence_shape_invalid",
        repr(sorted(evidence)),
    )
    for key in (
        "run_id",
        "manifest_ref",
        "current_kg_generation_id",
        "report_ref",
        "report_id",
        "quarantine_id",
        "graph_schema_version",
    ):
        candidate = evidence.get(key)
        _require(
            isinstance(candidate, str)
            and 0 < len(candidate) <= 1024
            and "\x00" not in candidate,
            "rehearsal_terminal_identity_invalid",
            key,
        )
    _require(
        _is_sha256(evidence.get("source_set_hash"))
        and _is_sha256(evidence.get("report_source_hash")),
        "rehearsal_terminal_source_hash_invalid",
    )
    _require(
        evidence.get("publishable_status") == "completed"
        and evidence.get("promotion_outcome") == "promoted"
        and evidence.get("event_emitted") is True
        and evidence.get("rehearsal_source_unchanged") is True,
        "rehearsal_terminal_outcome_invalid",
    )
    _require(
        type(evidence.get("orphan_count")) is int and evidence.get("orphan_count") == 0,
        "rehearsal_terminal_orphan_count_invalid",
    )
    _require(
        str(evidence.get("rehearsal_source_home")) == str(source_home),
        "rehearsal_terminal_source_home_mismatch",
    )
    storage_before = _validate_storage_hashes(
        evidence.get("sqlite_storage_before"),
        code="rehearsal_terminal_storage_before_invalid",
    )
    _validate_storage_hashes(
        evidence.get("sqlite_storage_after"),
        code="rehearsal_terminal_storage_after_invalid",
    )
    board_storage_post_teardown = _validate_board_storage_hashes(
        evidence.get("board_storage_post_teardown"),
        code="rehearsal_terminal_board_storage_invalid",
    )
    _require(
        evidence.get("board_storage_post_teardown_sha256")
        == _canonical_json_hash(board_storage_post_teardown),
        "rehearsal_terminal_board_storage_digest_mismatch",
    )
    _require(
        storage_before == dict(source_storage),
        "rehearsal_terminal_initial_storage_mismatch",
    )
    # Round-trip through the canonical encoder now, while still under the
    # recovery lock, so the terminal digest cannot depend on custom objects.
    return _strict_json_mapping(
        _canonical_json_bytes(evidence),
        code="rehearsal_terminal_evidence",
    )


_REHEARSAL_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "issued_at",
        "expires_at",
        "board_id",
        "source_data_home",
        "receipt_path",
        "execution_contract",
        "install_fingerprint",
        "executor_sha256",
        "entrypoints_sha256",
        "source_scope_sha256",
        "source_scope_file_count",
        "source_schema_sha256",
        "source_sqlite_storage",
        "terminal_evidence",
        "terminal_result_sha256",
        "attestation_sha256",
    }
)


def _build_rehearsal_attestation(
    *,
    board_id: str,
    source_home: Path,
    source_snapshot: Mapping[str, str],
    source_schema: str,
    source_storage: Mapping[str, str],
    terminal: Mapping[str, Any],
    install_fingerprint: str,
    receipt_path: Path,
    execution_contract: Mapping[str, Any],
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    issued = (issued_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = issued + timedelta(seconds=REHEARSAL_RECEIPT_TTL_SECONDS)
    terminal_evidence = _validate_rehearsal_terminal_evidence(
        terminal,
        source_home=source_home,
        source_storage=source_storage,
    )
    body: dict[str, Any] = {
        "schema_version": REHEARSAL_RECEIPT_SCHEMA,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "board_id": board_id,
        "source_data_home": str(source_home),
        "receipt_path": str(receipt_path),
        "execution_contract": dict(execution_contract),
        "install_fingerprint": install_fingerprint,
        "executor_sha256": _hash_executor_file(),
        "entrypoints_sha256": _hash_recovery_entrypoint_metadata(),
        "source_scope_sha256": _canonical_json_hash(dict(source_snapshot)),
        "source_scope_file_count": len(source_snapshot),
        "source_schema_sha256": source_schema,
        "source_sqlite_storage": dict(source_storage),
        "terminal_evidence": terminal_evidence,
        "terminal_result_sha256": _canonical_json_hash(terminal_evidence),
    }
    _require(
        all(
            _is_sha256(body[key])
            for key in (
                "install_fingerprint",
                "executor_sha256",
                "entrypoints_sha256",
                "source_scope_sha256",
                "source_schema_sha256",
                "terminal_result_sha256",
            )
        ),
        "rehearsal_attestation_digest_invalid",
    )
    body["attestation_sha256"] = _canonical_json_hash(body)
    return body


def _validate_rehearsal_attestation(
    *,
    receipt_path: Path,
    bound_receipt_path: Path | None = None,
    data_home: Path,
    board_id: str,
    install_fingerprint: str,
    execution_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    raw = _read_bounded_stable_file(receipt_path, code="rehearsal_receipt")
    receipt_file_hash = hashlib.sha256(raw).hexdigest()
    receipt = _strict_json_mapping(raw, code="rehearsal_receipt")
    _require(
        set(receipt) == _REHEARSAL_RECEIPT_FIELDS,
        "rehearsal_receipt_shape_invalid",
        repr(sorted(receipt)),
    )
    supplied_attestation_hash = receipt.get("attestation_sha256")
    _require(_is_sha256(supplied_attestation_hash), "rehearsal_receipt_hash_invalid")
    body = dict(receipt)
    del body["attestation_sha256"]
    _require(
        _canonical_json_hash(body) == supplied_attestation_hash,
        "rehearsal_receipt_integrity_mismatch",
    )
    _require(
        receipt.get("schema_version") == REHEARSAL_RECEIPT_SCHEMA,
        "rehearsal_receipt_schema_invalid",
    )
    issued = _parse_utc_timestamp(
        receipt.get("issued_at"),
        code="rehearsal_receipt_issued_at_invalid",
    )
    expires = _parse_utc_timestamp(
        receipt.get("expires_at"),
        code="rehearsal_receipt_expires_at_invalid",
    )
    _require(
        expires - issued == timedelta(seconds=REHEARSAL_RECEIPT_TTL_SECONDS),
        "rehearsal_receipt_ttl_invalid",
    )
    now = datetime.now(timezone.utc)
    _require(
        issued <= now + timedelta(seconds=30),
        "rehearsal_receipt_issued_in_future",
    )
    _require(now < expires, "rehearsal_receipt_expired")
    _require(receipt.get("board_id") == board_id, "rehearsal_receipt_board_mismatch")
    _require(
        receipt.get("source_data_home") == str(data_home),
        "rehearsal_receipt_data_home_mismatch",
    )
    _require(
        receipt.get("receipt_path")
        == str(bound_receipt_path if bound_receipt_path is not None else receipt_path),
        "rehearsal_receipt_path_mismatch",
    )
    _require(
        receipt.get("execution_contract") == dict(execution_contract),
        "rehearsal_receipt_execution_contract_mismatch",
    )
    _require(
        receipt.get("install_fingerprint") == install_fingerprint,
        "rehearsal_receipt_install_mismatch",
    )
    _require(
        receipt.get("executor_sha256") == _hash_executor_file(),
        "rehearsal_receipt_executor_mismatch",
    )
    _require(
        receipt.get("entrypoints_sha256") == _hash_recovery_entrypoint_metadata(),
        "rehearsal_receipt_entrypoint_mismatch",
    )
    before = _rehearsal_scope_snapshot(data_home, board_id)
    current_schema = _sqlite_schema_fingerprint(data_home / "data" / "pulse.db")
    current_storage = _sqlite_storage_fingerprints(data_home / "data" / "pulse.db")
    after = _rehearsal_scope_snapshot(data_home, board_id)
    _require(before == after, "live_recovery_scope_changed_during_attestation_check")
    _require(
        receipt.get("source_scope_sha256") == _canonical_json_hash(before)
        and type(receipt.get("source_scope_file_count")) is int
        and receipt.get("source_scope_file_count") == len(before),
        "rehearsal_receipt_source_scope_mismatch",
    )
    _require(
        receipt.get("source_schema_sha256") == current_schema,
        "rehearsal_receipt_source_schema_mismatch",
    )
    expected_storage = _validate_storage_hashes(
        receipt.get("source_sqlite_storage"),
        code="rehearsal_receipt_source_storage_invalid",
    )
    _require(
        expected_storage == current_storage,
        "rehearsal_receipt_source_storage_mismatch",
    )
    terminal = _validate_rehearsal_terminal_evidence(
        receipt.get("terminal_evidence"),
        source_home=data_home,
        source_storage=current_storage,
    )
    _require(
        receipt.get("terminal_result_sha256") == _canonical_json_hash(terminal),
        "rehearsal_receipt_terminal_digest_mismatch",
    )
    _require(
        datetime.now(timezone.utc) < expires,
        "rehearsal_receipt_expired_during_validation",
    )
    return receipt, receipt_file_hash


def _register_live_consumption(
    *,
    receipt_path: Path,
    receipt: Mapping[str, Any],
    receipt_file_hash: str,
    data_home: Path,
) -> Path:
    current_bytes = _read_bounded_stable_file(
        receipt_path,
        code="rehearsal_receipt",
    )
    _require(
        hashlib.sha256(current_bytes).hexdigest() == receipt_file_hash
        and _strict_json_mapping(current_bytes, code="rehearsal_receipt")
        == dict(receipt),
        "rehearsal_receipt_changed_before_consumption",
    )
    consumed_path = _resolve_external_new_file(
        str(receipt_path.with_name(f"{receipt_path.name}.live-consumed.json")),
        protected_homes=(data_home,),
        purpose="live_consumed_receipt",
    )
    _consume_file_durable(
        receipt_path,
        consumed_path,
        expected_content=current_bytes,
        code="live_consumed_receipt",
    )
    return consumed_path


def _assert_required_recovery_schema(db_path: Path) -> None:
    """Read-only, version-pinned prerequisite check; never repairs/migrates."""

    with _sqlite_connect_readonly(db_path) as connection:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        foreign_key_violation = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
    _require(
        user_version == EXPECTED_SQLITE_USER_VERSION,
        "sqlite_user_version_mismatch",
        f"expected={EXPECTED_SQLITE_USER_VERSION} actual={user_version}",
    )
    _require(schema_version > 0, "sqlite_schema_version_missing")
    _require(
        foreign_key_violation is None,
        "sqlite_foreign_key_check_failed",
        repr(foreign_key_violation),
    )
    for table, required in REQUIRED_RECOVERY_COLUMNS.items():
        actual = frozenset(_table_columns(db_path, table))
        missing = sorted(required - actual)
        _require(not missing, "recovery_schema_columns_missing", f"{table}:{missing}")
    app_settings_info = _table_info(db_path, "app_settings")
    _require(
        app_settings_info == EXPECTED_APP_SETTINGS_TABLE_INFO,
        "recovery_app_settings_schema_mismatch",
        f"expected={EXPECTED_APP_SETTINGS_TABLE_INFO!r} actual={app_settings_info!r}",
    )


def _assert_full_orm_schema_present(db_path: Path) -> None:
    """Require every installed Community ORM table/column in the frozen DB."""

    from okto_pulse.community.adapters import sqlalchemy_models as _models
    from okto_pulse.community.adapters.sqlalchemy_base import Base

    _require(_models is not None, "community_models_import_failed")
    expected_tables = tuple(
        sorted(Base.metadata.tables.values(), key=lambda table: str(table.name))
    )
    _require(bool(expected_tables), "community_orm_metadata_empty")
    for table in expected_tables:
        actual = frozenset(_table_columns(db_path, str(table.name)))
        expected = frozenset(str(column.name) for column in table.columns)
        missing = sorted(expected - actual)
        _require(
            not missing,
            "installed_schema_not_fully_migrated",
            f"{table.name}:{missing}",
        )


def _assert_schema_unchanged(db_path: Path, expected_fingerprint: str) -> None:
    _require(
        _sqlite_schema_fingerprint(db_path) == expected_fingerprint,
        "relational_schema_changed_during_recovery",
    )
    with _sqlite_connect_readonly(db_path) as connection:
        violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    _require(
        violation is None,
        "terminal_sqlite_foreign_key_check_failed",
        repr(violation),
    )


def _snapshot_query(
    db_path: Path,
    query: str,
    parameters: Sequence[Any] = (),
) -> QueueSnapshot:
    with _sqlite_connect_readonly(db_path) as connection:
        cursor = connection.execute(query, tuple(parameters))
        columns = tuple(str(item[0]) for item in (cursor.description or ()))
        rows = _bounded_sqlite_snapshot_rows(cursor, code="sqlite_snapshot")
    return QueueSnapshot(columns, rows, _fingerprint_rows(columns, rows))


def _assert_database_preflight(
    db_path: Path,
    board_id: str,
) -> tuple[str, dict[str, Any], str]:
    _require(db_path.is_file(), "pulse_database_missing", str(db_path))
    _assert_required_recovery_schema(db_path)
    schema_fingerprint = _sqlite_schema_fingerprint(db_path)
    with _sqlite_connect_readonly(db_path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        _require(
            integrity is not None and str(integrity[0]).lower() == "ok",
            "sqlite_integrity_check_failed",
            repr(integrity),
        )
        row = connection.execute(
            "SELECT owner_id, settings FROM boards WHERE id=?",
            (board_id,),
        ).fetchone()
    _require(row is not None, "board_not_found", board_id)
    owner_id = str(row[0])
    settings: dict[str, Any] = {}
    raw_settings = row[1]
    if isinstance(raw_settings, str) and raw_settings:
        try:
            decoded = json.loads(raw_settings)
        except json.JSONDecodeError as exc:
            raise RecoveryRefused("board_settings_invalid") from exc
        _require(isinstance(decoded, Mapping), "board_settings_invalid")
        settings = dict(decoded)
    elif isinstance(raw_settings, Mapping):
        settings = dict(raw_settings)
    _require(
        not bool(settings.get("dlq_auto_drain_enabled", False)),
        "dlq_auto_drain_must_be_disabled",
    )
    # A short write reservation proves that no process bypassing the serve lock
    # currently owns SQLite.  It is released without writing.
    with sqlite3.connect(str(db_path), timeout=2.0) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    return owner_id, settings, schema_fingerprint


def _dlq_snapshot(db_path: Path) -> QueueSnapshot:
    columns = _table_columns(db_path, "consolidation_dead_letter")
    if not columns:
        return QueueSnapshot((), (), hashlib.sha256(b"empty-dlq").hexdigest())
    projection = ", ".join(f'"{column}"' for column in columns)
    return _snapshot_query(
        db_path,
        f"SELECT {projection} FROM consolidation_dead_letter ORDER BY id",
    )


def _dlq_ids_for_board(db_path: Path, board_id: str) -> tuple[str, ...]:
    if not _table_columns(db_path, "consolidation_dead_letter"):
        return ()
    snapshot = _snapshot_query(
        db_path,
        "SELECT id FROM consolidation_dead_letter WHERE board_id=? ORDER BY id",
        (board_id,),
    )
    return tuple(str(row[0]) for row in snapshot.rows)


def _non_target_queue_snapshot(
    db_path: Path,
    *,
    board_id: str,
    source: str,
) -> QueueSnapshot:
    columns = _table_columns(db_path, "consolidation_queue")
    _require(bool(columns), "consolidation_queue_schema_missing")
    projection = ", ".join(f'"{column}"' for column in columns)
    return _snapshot_query(
        db_path,
        f"SELECT {projection} FROM consolidation_queue "
        "WHERE NOT (board_id=? AND source=? AND work_kind='consolidate') "
        "ORDER BY id",
        (board_id, source),
    )


def _full_queue_snapshot(db_path: Path) -> QueueSnapshot:
    columns = _table_columns(db_path, "consolidation_queue")
    _require(bool(columns), "consolidation_queue_schema_missing")
    projection = ", ".join(f'"{column}"' for column in columns)
    return _snapshot_query(
        db_path,
        f"SELECT {projection} FROM consolidation_queue ORDER BY id",
    )


def _protected_queue_snapshot(
    db_path: Path,
    *,
    board_id: str,
    admitted_identities: set[tuple[str, str]],
) -> QueueSnapshot:
    """Protect every queue row admission is not explicitly allowed to alter."""

    snapshot = _full_queue_snapshot(db_path)
    indexes = {name: offset for offset, name in enumerate(snapshot.columns)}
    required = {"board_id", "artifact_type", "artifact_id", "work_kind"}
    _require(required <= indexes.keys(), "consolidation_queue_schema_missing")
    protected: list[tuple[Any, ...]] = []
    for row in snapshot.rows:
        is_adoptable = bool(
            str(row[indexes["board_id"]]) == board_id
            and str(row[indexes["work_kind"]]) == "consolidate"
            and (
                str(row[indexes["artifact_type"]]),
                str(row[indexes["artifact_id"]]),
            )
            in admitted_identities
        )
        if not is_adoptable:
            protected.append(row)
    rows = tuple(protected)
    return QueueSnapshot(
        snapshot.columns,
        rows,
        _fingerprint_rows(snapshot.columns, rows),
    )


def _assert_compensation_queue_transition(
    baseline: QueueSnapshot,
    terminal: QueueSnapshot,
    *,
    board_id: str,
    source: str,
) -> None:
    """Prove the exact Community compensation transition for every queue row."""

    _require(
        terminal.columns == baseline.columns,
        "manifest_compensation_queue_shape_changed",
    )
    indexes = {name: offset for offset, name in enumerate(baseline.columns)}
    required = {
        "id",
        "board_id",
        "status",
        "source",
        "work_kind",
        "attempts",
        "last_error",
        "next_retry_at",
        "claimed_by_session_id",
        "claim_token",
        "claimed_at",
        "worker_id",
        "claim_timeout_at",
        "triggered_at",
        "payload",
    }
    _require(required <= indexes.keys(), "consolidation_queue_schema_missing")
    before_by_id = {str(row[indexes["id"]]): row for row in baseline.rows}
    after_by_id = {str(row[indexes["id"]]): row for row in terminal.rows}
    _require(
        len(before_by_id) == len(baseline.rows)
        and len(after_by_id) == len(terminal.rows)
        and set(after_by_id) == set(before_by_id),
        "manifest_compensation_queue_identity_set_changed",
    )
    for row_id, before in before_by_id.items():
        after = after_by_id[row_id]
        target_active = bool(
            str(before[indexes["board_id"]]) == board_id
            and str(before[indexes["source"]] or "") == source
            and str(before[indexes["work_kind"]]) == "consolidate"
            and str(before[indexes["status"]]) in {"pending", "claimed"}
        )
        if not target_active:
            _require(
                after == before,
                "manifest_compensation_unrelated_queue_row_changed",
                row_id,
            )
            continue
        raw_payload = before[indexes["payload"]]
        try:
            payload = (
                json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryRefused(
                f"manifest_compensation_queue_payload_invalid:{row_id}"
            ) from exc
        marker = (
            payload.get("_rebuild_deferred_live")
            if isinstance(payload, Mapping)
            else None
        )
        expected = dict(zip(baseline.columns, before, strict=True))
        for field in (
            "claimed_by_session_id",
            "claim_token",
            "claimed_at",
            "worker_id",
            "claim_timeout_at",
            "next_retry_at",
        ):
            expected[field] = None
        dynamic_triggered_at = False
        if isinstance(marker, Mapping):
            live_source = str(marker.get("source") or "").strip()
            live_payload = marker.get("payload")
            _require(
                bool(live_source)
                and not live_source.startswith("rebuild:")
                and (live_payload is None or isinstance(live_payload, Mapping)),
                "manifest_compensation_deferred_live_marker_invalid",
                row_id,
            )
            expected.update(
                {
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "source": live_source,
                    "payload": (
                        json.dumps(
                            dict(live_payload),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if live_payload is not None
                        else None
                    ),
                }
            )
            if "triggered_by_event" in indexes:
                expected["triggered_by_event"] = marker.get("triggered_by_event")
            dynamic_triggered_at = True
        else:
            expected.update(
                {
                    "status": "failed",
                    "last_error": "rebuild_compensated",
                }
            )
        actual = dict(zip(terminal.columns, after, strict=True))
        if dynamic_triggered_at:
            _parse_queue_timestamp(actual.pop("triggered_at"))
            expected.pop("triggered_at")
        _require(
            actual == expected,
            "manifest_compensation_target_queue_transition_invalid",
            row_id,
        )


def _assert_protected_admission_is_noop(
    db_path: Path,
    *,
    board_id: str,
    admitted_identities: set[tuple[str, str]],
    authorized_rebuild_source: str,
    compensated_adoption: CompensatedQueueAdoption | None = None,
) -> None:
    """Refuse before issue/consume if v4 admission would alter unrelated work."""

    snapshot = _full_queue_snapshot(db_path)
    indexes = {name: offset for offset, name in enumerate(snapshot.columns)}
    required = {
        "id",
        "board_id",
        "artifact_type",
        "artifact_id",
        "work_kind",
        "status",
        "source",
        "claimed_by_session_id",
        "claim_token",
        "claimed_at",
        "worker_id",
        "claim_timeout_at",
        "next_retry_at",
        "last_error",
        "payload",
    }
    _require(required <= indexes.keys(), "consolidation_queue_schema_missing")
    for row in snapshot.rows:
        if str(row[indexes["board_id"]]) != board_id:
            continue
        identity = (
            str(row[indexes["artifact_type"]]),
            str(row[indexes["artifact_id"]]),
        )
        if (
            str(row[indexes["work_kind"]]) == "consolidate"
            and identity in admitted_identities
        ):
            row_id = str(row[indexes["id"]])
            status = str(row[indexes["status"]])
            source = str(row[indexes["source"]] or "")
            compensated_tombstone = bool(
                compensated_adoption is not None
                and source == compensated_adoption.source
                and identity in compensated_adoption.identities
            )
            _require(
                not source.startswith("rebuild:")
                or source == authorized_rebuild_source
                or compensated_tombstone,
                "adoptable_identity_owned_by_other_rebuild",
                row_id,
            )
            if status == "claimed":
                _require(
                    source == authorized_rebuild_source
                    and all(
                        row[indexes[column]] is not None
                        for column in (
                            "claimed_by_session_id",
                            "claim_token",
                            "claimed_at",
                            "worker_id",
                            "claim_timeout_at",
                        )
                    ),
                    "adoptable_identity_claim_not_exact_recovery",
                    row_id,
                )
            else:
                if compensated_tombstone:
                    raw_payload = row[indexes["payload"]]
                    try:
                        payload = (
                            json.loads(raw_payload)
                            if isinstance(raw_payload, str)
                            else raw_payload
                        )
                    except (TypeError, ValueError) as exc:
                        raise RecoveryRefused(
                            f"compensated_tombstone_payload_invalid:{row_id}"
                        ) from exc
                    expected_membership = (
                        compensated_adoption.membership_by_identity.get(identity)
                    )
                    _require(
                        status == "failed"
                        and str(row[indexes["last_error"]] or "")
                        == "rebuild_compensated"
                        and all(
                            row[indexes[column]] is None
                            for column in (
                                "claimed_by_session_id",
                                "claim_token",
                                "claimed_at",
                                "worker_id",
                                "claim_timeout_at",
                                "next_retry_at",
                            )
                        )
                        and isinstance(payload, Mapping)
                        and dict(payload)
                        == {"_rebuild_membership": dict(expected_membership or {})},
                        "compensated_tombstone_not_exact",
                        row_id,
                    )
                    continue
                _require(
                    status == "pending"
                    and all(
                        row[indexes[column]] is None
                        for column in (
                            "claimed_by_session_id",
                            "claim_token",
                            "claimed_at",
                            "worker_id",
                            "claim_timeout_at",
                        )
                    ),
                    "adoptable_identity_not_clean_pending",
                    row_id,
                )
            continue
        if str(row[indexes["status"]]) not in {"pending", "claimed"}:
            continue
        row_id = str(row[indexes["id"]])
        _require(
            str(row[indexes["status"]]) == "pending",
            "unrelated_claim_requires_external_reconciliation",
            row_id,
        )
        _require(
            not str(row[indexes["source"]] or "").startswith("rebuild:"),
            "unrelated_rebuild_row_requires_external_reconciliation",
            row_id,
        )
        _require(
            all(
                row[indexes[column]] is None
                for column in (
                    "claimed_by_session_id",
                    "claim_token",
                    "claimed_at",
                    "worker_id",
                    "claim_timeout_at",
                    "next_retry_at",
                )
            ),
            "unrelated_queue_row_would_be_mutated_by_admission",
            row_id,
        )


def _exact_queue_rows(db_path: Path, *, board_id: str, source: str) -> QueueSnapshot:
    return _snapshot_query(
        db_path,
        "SELECT id, artifact_type, artifact_id, status, priority, attempts, "
        "last_error, next_retry_at, claimed_at, claim_timeout_at, worker_id, "
        "claimed_by_session_id, claim_token, generation, delete_event_id, work_kind, "
        "source, triggered_at, payload "
        "FROM consolidation_queue WHERE board_id=? AND source=? "
        "AND work_kind='consolidate' ORDER BY triggered_at, id",
        (board_id, source),
    )


def _active_exact_queue_depth(db_path: Path, *, board_id: str, source: str) -> int:
    snapshot = _snapshot_query(
        db_path,
        "SELECT COUNT(*) FROM consolidation_queue WHERE board_id=? AND source=? "
        "AND work_kind='consolidate' AND status IN ('pending','claimed')",
        (board_id, source),
    )
    return int(snapshot.rows[0][0])


def _active_rebuild_queue_depth(db_path: Path, *, board_id: str) -> int:
    snapshot = _snapshot_query(
        db_path,
        "SELECT COUNT(*) FROM consolidation_queue WHERE board_id=? "
        "AND source LIKE 'rebuild:%' AND status IN ('pending','claimed')",
        (board_id,),
    )
    return int(snapshot.rows[0][0])


@contextmanager
def _open_snapshot_file_exclusive(path: Path):  # noqa: ANN201
    """Open one snapshot file while refusing a concurrent native handle."""

    if os.name != "nt":
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(path, flags)
        try:
            with os.fdopen(fd, "rb", closefd=True) as handle:
                fd = -1
                yield handle
        finally:
            if fd >= 0:
                os.close(fd)
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    generic_read = 0x80000000
    open_existing = 3
    sequential_scan = 0x08000000
    open_reparse_point = 0x00200000
    native_handle = create_file(
        os.path.abspath(path),
        generic_read,
        0,  # No FILE_SHARE_* flags: an extant native handle is a refusal.
        None,
        open_existing,
        sequential_scan | open_reparse_point,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if native_handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    fd: int | None = None
    try:
        fd = msvcrt.open_osfhandle(int(native_handle), os.O_RDONLY)
        native_handle = None
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = None
            yield handle
    finally:
        if fd is not None:
            os.close(fd)
        if native_handle not in (None, invalid_handle):
            close_handle(native_handle)


def _enumerate_snapshot_entries(root: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    """Enumerate without ``Path.rglob`` suppressing subtree access failures."""

    pending = [root]
    entries: list[tuple[Path, os.stat_result]] = []
    while pending:
        directory = pending.pop()
        relative_directory = (
            "." if directory == root else directory.relative_to(root).as_posix()
        )
        try:
            directory_details = directory.lstat()
            if directory.is_symlink() or _stat_is_filesystem_alias(directory_details):
                raise RecoveryRefused(
                    f"recovery_artifact_alias_refused:relative={relative_directory!r}"
                )
            if not stat.S_ISDIR(directory_details.st_mode):
                raise RecoveryRefused(
                    f"recovery_artifact_type_refused:relative={relative_directory!r}"
                )
            with os.scandir(directory) as iterator:
                scanned = sorted(iterator, key=lambda entry: entry.name)
        except RecoveryRefused:
            raise
        except OSError as exc:
            raise RecoveryRefused(
                "recovery_artifact_enumeration_failed:"
                f"relative_dir={relative_directory!r};"
                f"errno={getattr(exc, 'errno', None)!r};"
                f"winerror={getattr(exc, 'winerror', None)!r}"
            ) from exc
        for entry in scanned:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RecoveryRefused(
                    "recovery_artifact_unverifiable:"
                    f"relative={relative!r};"
                    f"errno={getattr(exc, 'errno', None)!r};"
                    f"winerror={getattr(exc, 'winerror', None)!r}"
                ) from exc
            _require(
                not entry.is_symlink() and not _stat_is_filesystem_alias(details),
                "recovery_artifact_alias_refused",
                relative,
            )
            _require(
                stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode),
                "recovery_artifact_type_refused",
                relative,
            )
            entries.append((path, details))
            if stat.S_ISDIR(details.st_mode):
                pending.append(path)
    return tuple(sorted(entries, key=lambda item: item[0].relative_to(root).as_posix()))


def _snapshot_stat_identity(details: os.stat_result) -> tuple[int, ...]:
    return tuple(
        int(getattr(details, field))
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_nlink",
        )
    )


def _snapshot_authoritative_entries(
    root: Path,
) -> tuple[tuple[Path, os.stat_result], ...]:
    authoritative: list[tuple[Path, os.stat_result]] = []
    for path, details in _enumerate_snapshot_entries(root):
        relative = path.relative_to(root).as_posix()
        try:
            baseline = path.lstat()
        except OSError as exc:
            raise RecoveryRefused(
                "recovery_artifact_unverifiable:"
                f"relative={relative!r};"
                f"errno={getattr(exc, 'errno', None)!r};"
                f"winerror={getattr(exc, 'winerror', None)!r}"
            ) from exc
        _require(
            not path.is_symlink() and not _stat_is_filesystem_alias(baseline),
            "recovery_artifact_alias_refused",
            relative,
        )
        _require(
            stat.S_ISDIR(baseline.st_mode) or stat.S_ISREG(baseline.st_mode),
            "recovery_artifact_type_refused",
            relative,
        )
        # Windows' DirEntry.stat can expose placeholder identity fields and a
        # stale directory mtime even without a concurrent mutation.  Use it
        # only to classify the entry during traversal; lstat is authoritative.
        _require(
            stat.S_IFMT(details.st_mode) == stat.S_IFMT(baseline.st_mode),
            "recovery_artifact_type_changed_before_hashing",
            relative,
        )
        authoritative.append((path, baseline))
    return tuple(authoritative)


def _snapshot_tree_identities(root: Path) -> dict[str, tuple[int, ...]]:
    return {
        path.relative_to(root).as_posix(): _snapshot_stat_identity(details)
        for path, details in _snapshot_authoritative_entries(root)
    }


def _snapshot_tree_hashes_once(
    root: Path,
) -> tuple[dict[str, str], dict[str, tuple[int, ...]]]:
    result: dict[str, str] = {}
    identities: dict[str, tuple[int, ...]] = {}
    for path, baseline in _snapshot_authoritative_entries(root):
        relative = path.relative_to(root).as_posix()
        identities[relative] = _snapshot_stat_identity(baseline)
        if stat.S_ISREG(baseline.st_mode):
            diagnostic = (
                f"relative={relative!r};type=regular;size={int(baseline.st_size)};"
                f"mode={int(baseline.st_mode):#o};"
                f"attrs={getattr(baseline, 'st_file_attributes', None)!r};"
                f"reparse_tag={getattr(baseline, 'st_reparse_tag', None)!r}"
            )
            digest = hashlib.sha256()
            try:
                with _open_snapshot_file_exclusive(path) as handle:
                    opened_before = os.fstat(handle.fileno())
                    _require(
                        stat.S_ISREG(opened_before.st_mode),
                        "recovery_artifact_type_changed_while_opening",
                        diagnostic,
                    )
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                    opened_after = os.fstat(handle.fileno())
                after = path.lstat()
            except RecoveryRefused:
                raise
            except OSError as exc:
                errno = getattr(exc, "errno", None)
                winerror = getattr(exc, "winerror", None)
                raise RecoveryRefused(
                    "recovery_artifact_read_failed:"
                    f"{diagnostic};errno={errno!r};winerror={winerror!r}"
                ) from exc
            _require(
                not path.is_symlink() and not _stat_is_filesystem_alias(after),
                "recovery_artifact_alias_changed_while_hashing",
                diagnostic,
            )
            _require(
                _snapshot_stat_identity(baseline)
                == _snapshot_stat_identity(opened_before)
                == _snapshot_stat_identity(opened_after)
                == _snapshot_stat_identity(after),
                "recovery_artifact_changed_while_hashing",
                diagnostic,
            )
            result[relative] = digest.hexdigest()
    return result, identities


def _snapshot_tree_hashes(root: Path) -> dict[str, str]:
    _assert_no_symlink_components(root, "recovery_artifact_alias_refused")
    try:
        root_details = root.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RecoveryRefused(f"recovery_artifact_root_unverifiable:{root}") from exc
    _require(
        not root.is_symlink() and not _stat_is_filesystem_alias(root_details),
        "recovery_artifact_alias_refused",
        str(root),
    )
    _require(
        stat.S_ISDIR(root_details.st_mode),
        "recovery_artifact_root_not_directory",
        str(root),
    )
    root_identity = _snapshot_stat_identity(root_details)
    first_hashes, first_identities = _snapshot_tree_hashes_once(root)
    second_hashes, second_identities = _snapshot_tree_hashes_once(root)
    terminal_identities = _snapshot_tree_identities(root)
    try:
        terminal_root_details = root.lstat()
    except OSError as exc:
        raise RecoveryRefused("recovery_artifact_root_changed_while_hashing") from exc
    _require(
        not root.is_symlink()
        and not _stat_is_filesystem_alias(terminal_root_details)
        and stat.S_ISDIR(terminal_root_details.st_mode)
        and root_identity == _snapshot_stat_identity(terminal_root_details)
        and first_identities == second_identities
        and second_identities == terminal_identities
        and first_hashes == second_hashes,
        "recovery_artifact_tree_changed_while_hashing",
    )
    return second_hashes


def _capture_post_teardown_board_storage(
    *,
    data_home: Path,
    board_id: str,
) -> tuple[dict[str, str], str]:
    """Prove the final native handle is closed and bind its physical bytes."""

    snapshot = _validate_board_storage_hashes(
        _snapshot_tree_hashes(data_home / "boards" / board_id),
        code="post_teardown_board_storage_invalid",
    )
    digest = _canonical_json_hash(snapshot)
    _emit(
        "post_teardown_board_storage_captured",
        board_id=board_id,
        file_count=len(snapshot),
        snapshot_sha256=digest,
    )
    return snapshot, digest


def _assert_no_rebuild_transients(baseline: Mapping[str, str]) -> None:
    transient_paths = sorted(
        relative for relative in baseline if Path(relative).name.endswith(".tmp")
    )
    _require(
        not transient_paths,
        "preexisting_rebuild_transient_refused",
        repr(transient_paths),
    )


def _assert_tree_preserved(root: Path, baseline: Mapping[str, str]) -> None:
    for relative, expected in baseline.items():
        path = root / relative
        _require(path.is_file(), "preexisting_quarantine_file_missing", str(path))
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        _require(
            actual == expected,
            "preexisting_quarantine_file_changed",
            relative,
        )


def _quarantine_ids(root: Path) -> frozenset[str]:
    _assert_no_symlink_components(root, "quarantine_root_alias_refused")
    try:
        root_details = root.lstat()
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise RecoveryRefused(f"quarantine_root_unverifiable:{root}") from exc
    _require(
        not root.is_symlink() and not _stat_is_filesystem_alias(root_details),
        "quarantine_root_alias_refused",
        str(root),
    )
    _require(
        stat.S_ISDIR(root_details.st_mode),
        "quarantine_root_not_directory",
        str(root),
    )
    result: set[str] = set()
    for entry in root.iterdir():
        try:
            details = entry.lstat()
        except OSError as exc:
            raise RecoveryRefused(f"quarantine_entry_unverifiable:{entry}") from exc
        _require(
            not entry.is_symlink() and not _stat_is_filesystem_alias(details),
            "quarantine_symlink_refused",
            str(entry),
        )
        if stat.S_ISDIR(details.st_mode) and entry.name.startswith("q_"):
            result.add(entry.name)
    return frozenset(result)


def _expected_compensation_board_storage(
    checkpoint: Mapping[str, Any] | None,
    *,
    quarantine_root: Path,
    board_id: str,
    manifest_ref: str,
) -> dict[str, str]:
    """Pin the exact predecessor bytes a governed compensation must restore."""

    if checkpoint is None:
        return {}
    receipts = checkpoint.get("receipts")
    quarantine_key = f"f06:{manifest_ref}:quarantine"
    quarantine_receipt = (
        receipts.get(quarantine_key) if isinstance(receipts, Mapping) else None
    )
    if not isinstance(quarantine_receipt, Mapping):
        return {}
    details = quarantine_receipt.get("details")
    _require(
        isinstance(details, Mapping),
        "manifest_compensation_quarantine_receipt_invalid",
    )
    assert isinstance(details, Mapping)
    affected = tuple(str(value) for value in details.get("affected_files", ()))
    if not affected:
        return {}
    quarantine_id = str(details.get("quarantine_ref") or "")
    _require(
        bool(quarantine_id),
        "manifest_compensation_quarantine_reference_missing",
    )
    quarantine_dir = quarantine_root / quarantine_id
    manifest = _load_json_file(
        quarantine_dir / "manifest.json",
        "manifest_compensation_quarantine_manifest_invalid",
    )
    _require(
        str(manifest.get("quarantine_id")) == quarantine_id
        and str(manifest.get("board_id")) == board_id
        and set(str(value) for value in manifest.get("affected_paths_relative", ()))
        == set(affected),
        "manifest_compensation_quarantine_binding_invalid",
    )
    expected: dict[str, str] = {}
    for relative in affected:
        relative_path = Path(relative)
        _require(
            not relative_path.is_absolute()
            and ".." not in relative_path.parts
            and len(relative_path.parts) == 1,
            "manifest_compensation_quarantine_path_invalid",
            relative,
        )
        path = quarantine_dir / relative_path
        _require(
            path.is_file() and not _is_filesystem_alias(path),
            "manifest_compensation_quarantine_source_missing",
            relative,
        )
        expected[relative_path.as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return expected


def _load_json_file(path: Path, code: str) -> Mapping[str, Any]:
    _require(path.is_file(), code, str(path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryRefused(f"{code}:{path}") from exc
    _require(isinstance(payload, Mapping), code, str(path))
    assert isinstance(payload, Mapping)
    return payload


def _capture_cognitive_ledger_baseline(
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    board_id: str,
) -> CognitiveLedgerBaseline:
    """Capture every target-board ledger before the rebuild service starts.

    The production cognitive store selects prior terminal rows by inspecting
    all JSON records in this exact board directory.  A recovery proof must use
    the same closed input set; silently skipping an unreadable, oversized, or
    concurrently-created record could authorize the wrong carry-forward row.
    """

    from okto_pulse.core.kg.rebuild_audit import (
        CognitiveConsolidationItem,
        compute_cognitive_item_id,
    )

    relative_prefix = f"audit/cognitive_pending/{board_id}/"
    tracked_relatives = {
        relative
        for relative in rebuild_baseline
        if relative.startswith(relative_prefix)
        and "/" not in relative[len(relative_prefix) :]
        and relative.endswith(".json")
    }
    _require(
        len(tracked_relatives) <= MAX_COGNITIVE_BASELINE_RECORDS,
        "cognitive_baseline_record_limit_exceeded",
        str(len(tracked_relatives)),
    )

    board_root = rebuild_root / "audit" / "cognitive_pending" / board_id
    disk_relatives: set[str] = set()
    if board_root.exists():
        _require(
            board_root.is_dir() and not _is_filesystem_alias(board_root),
            "cognitive_baseline_directory_invalid",
            str(board_root),
        )
        for entry in board_root.iterdir():
            _require(
                not _is_filesystem_alias(entry),
                "cognitive_baseline_artifact_alias_refused",
                str(entry),
            )
            if entry.is_file() and entry.suffix == ".json":
                disk_relatives.add(entry.relative_to(rebuild_root).as_posix())
    _require(
        disk_relatives == tracked_relatives,
        "cognitive_baseline_changed_before_capture",
        repr(sorted(disk_relatives ^ tracked_relatives)),
    )

    records: list[CognitiveLedgerRecord] = []
    seen_generations: set[str] = set()
    for relative in sorted(tracked_relatives):
        path = rebuild_root / relative
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RecoveryRefused(
                f"cognitive_baseline_artifact_unreadable:{relative}"
            ) from exc
        _require(
            0 < size <= MAX_COGNITIVE_BASELINE_RECORD_BYTES,
            "cognitive_baseline_record_size_invalid",
            f"{relative}:{size}",
        )
        expected_hash = rebuild_baseline[relative]
        _require(
            hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash,
            "cognitive_baseline_changed_while_capturing",
            relative,
        )
        payload = _load_json_file(path, "cognitive_baseline_record_invalid")
        generation_id = str(payload.get("kg_generation_id") or "")
        _require(
            bool(generation_id)
            and relative == f"{relative_prefix}{generation_id}.json"
            and str(payload.get("board_id") or "") == board_id
            and generation_id not in seen_generations,
            "cognitive_baseline_record_binding_invalid",
            relative,
        )
        seen_generations.add(generation_id)
        raw_items = payload.get("items")
        if raw_items is not None:
            _require(
                isinstance(raw_items, list),
                "cognitive_baseline_items_invalid",
                relative,
            )
            assert isinstance(raw_items, list)
            seen_sources: set[str] = set()
            for raw_item in raw_items:
                _require(
                    isinstance(raw_item, Mapping),
                    "cognitive_baseline_item_invalid",
                    relative,
                )
                assert isinstance(raw_item, Mapping)
                item = CognitiveConsolidationItem.from_dict(raw_item)
                _require(
                    bool(item.source_ref)
                    and item.source_ref not in seen_sources
                    and item.board_id == board_id
                    and bool(item.kg_generation_id)
                    and item.item_id
                    == compute_cognitive_item_id(
                        board_id,
                        item.kg_generation_id,
                        item.source_ref,
                    ),
                    "cognitive_baseline_item_binding_invalid",
                    f"{relative}:{item.source_ref}",
                )
                seen_sources.add(item.source_ref)
        frozen_payload = json.loads(_canonical_json_bytes(payload))
        assert isinstance(frozen_payload, Mapping)
        records.append((generation_id, frozen_payload))
        _require(
            hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash,
            "cognitive_baseline_changed_while_capturing",
            relative,
        )
    return CognitiveLedgerBaseline(records=tuple(records))


def _select_cognitive_carry_forward_rows(
    baseline: CognitiveLedgerBaseline,
    *,
    target_generation_id: str,
    expected_by_source: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Mirror ``CognitiveConsolidationItemStore._write_initial`` exactly."""

    from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItem

    records = {generation_id: payload for generation_id, payload in baseline.records}
    target = records.get(target_generation_id)
    target_items: dict[str, dict[str, Any]] = {}
    same_generation: dict[str, dict[str, Any]] = {}
    if target is not None:
        raw_items = target.get("items")
        if isinstance(raw_items, list):
            for raw_item in raw_items:
                assert isinstance(raw_item, Mapping)  # capture validated this
                normalized = CognitiveConsolidationItem.from_dict(raw_item).to_dict()
                source_ref = str(normalized["source_ref"])
                target_items[source_ref] = normalized
                if str(normalized.get("status") or "") not in {
                    "consolidated",
                    "skipped",
                }:
                    continue
                same_generation[source_ref] = normalized
    unrelated_same_generation = {
        source_ref: row
        for source_ref, row in target_items.items()
        if source_ref not in expected_by_source
    }
    # Production intentionally disables the entire prior-generation lookup as
    # soon as at least one same-generation consolidated/skipped row exists.
    if same_generation:
        return (
            {
                source_ref: row
                for source_ref, row in same_generation.items()
                if source_ref in expected_by_source
            },
            unrelated_same_generation,
        )

    ordered_records = sorted(
        (
            (
                str(payload.get("recorded_at") or ""),
                generation_id,
                payload,
            )
            for generation_id, payload in baseline.records
            if generation_id != target_generation_id
        ),
        key=lambda value: (value[0], value[1]),
        reverse=True,
    )
    newest_terminal: dict[str, dict[str, Any]] = {}
    for _recorded_at, _generation_id, payload in ordered_records:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            assert isinstance(raw_item, Mapping)  # capture validated this
            if str(raw_item.get("status") or "") not in {
                "consolidated",
                "skipped",
                "failed",
            }:
                continue
            source_ref = str(raw_item.get("source_ref") or "")
            if not source_ref or source_ref in newest_terminal:
                continue
            newest_terminal[source_ref] = CognitiveConsolidationItem.from_dict(
                raw_item
            ).to_dict()

    carry_forward: dict[str, dict[str, Any]] = {}
    for source_ref, prior in newest_terminal.items():
        expected = expected_by_source.get(source_ref)
        if expected is None:
            continue
        expected_hash = expected.get("content_hash")
        prior_hash = prior.get("content_hash")
        expected_hash_value = str(expected_hash) if expected_hash else None
        prior_hash_value = str(prior_hash) if prior_hash else None
        if (
            expected_hash_value is not None
            and prior_hash_value is not None
            and expected_hash_value == prior_hash_value
        ):
            carry_forward[source_ref] = prior
    return carry_forward, unrelated_same_generation


def _assert_governed_quarantine(
    *,
    quarantine_root: Path,
    baseline_hashes: Mapping[str, str],
    baseline_ids: frozenset[str],
    receipt: Mapping[str, Any],
    board_id: str,
    manifest_ref: str,
) -> str:
    _assert_tree_preserved(quarantine_root, baseline_hashes)
    details = receipt.get("details")
    _require(isinstance(details, Mapping), "quarantine_receipt_details_missing")
    assert isinstance(details, Mapping)
    quarantine_id = str(details.get("quarantine_ref") or "")
    _require(bool(quarantine_id), "quarantine_receipt_reference_missing")
    terminal_ids = _quarantine_ids(quarantine_root)
    new_ids = terminal_ids - baseline_ids
    if quarantine_id in baseline_ids:
        _require(
            not new_ids,
            "resume_created_second_quarantine",
            repr(sorted(new_ids)),
        )
    else:
        _require(
            new_ids == {quarantine_id},
            "governed_quarantine_count_invalid",
            repr(sorted(new_ids)),
        )
    new_paths = set(_snapshot_tree_hashes(quarantine_root)) - set(baseline_hashes)
    if quarantine_id in baseline_ids:
        _require(
            not new_paths,
            "resume_changed_quarantine_tree",
            repr(sorted(new_paths)),
        )
    else:
        _require(
            all(relative.startswith(f"{quarantine_id}/") for relative in new_paths),
            "unexpected_quarantine_artifact",
            repr(sorted(new_paths)),
        )
    manifest = _load_json_file(
        quarantine_root / quarantine_id / "manifest.json",
        "quarantine_manifest_missing_or_invalid",
    )
    _require(
        str(manifest.get("quarantine_id")) == quarantine_id, "quarantine_id_mismatch"
    )
    _require(str(manifest.get("board_id")) == board_id, "quarantine_board_mismatch")
    _require(
        str(manifest.get("graph_type")) == "board_graph",
        "quarantine_graph_type_invalid",
    )
    _require(
        str(manifest.get("reason")) == f"explicit_rebuild:{manifest_ref}",
        "quarantine_reason_mismatch",
    )
    _require(int(manifest.get("files_moved") or 0) >= 1, "quarantine_moved_nothing")
    affected = tuple(
        str(value) for value in manifest.get("affected_paths_relative", ())
    )
    _require(
        "graph.lbug" in affected, "quarantine_live_graph_not_captured", repr(affected)
    )
    return quarantine_id


def _payload_mentions_any(value: Any, identities: frozenset[str]) -> bool:
    if isinstance(value, Mapping):
        return any(_payload_mentions_any(item, identities) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_payload_mentions_any(item, identities) for item in value)
    if isinstance(value, str):
        return any(identity and identity in value for identity in identities)
    return False


_COGNITIVE_OVERLAY_REVISION_RELATIVE = (
    "global_discovery_recovery/cognitive_pending_overlay_revision.json"
)
_REBASELINE_AUDIT_RELATIVE = "rebaseline_audit/records.json"


def _validated_cognitive_overlay_revision(
    payload: Mapping[str, Any] | None,
    *,
    code: str,
) -> tuple[int, str]:
    _require(isinstance(payload, Mapping), code)
    assert isinstance(payload, Mapping)
    _require(
        set(payload) == {"version", "state", "revision", "nonce"},
        f"{code}_shape_invalid",
        repr(sorted(str(key) for key in payload)),
    )
    revision = payload.get("revision")
    nonce = payload.get("nonce")
    _require(
        payload.get("version") == 1
        and payload.get("state") == "stable"
        and type(revision) is int
        and int(revision) >= 0
        and isinstance(nonce, str)
        and 16 <= len(nonce) <= 128,
        f"{code}_value_invalid",
    )
    return int(revision), str(nonce)


def _assert_cognitive_overlay_revision_transition(
    *,
    rebuild_root: Path,
    baseline_payload: Mapping[str, Any] | None,
    expect_advance: bool,
) -> None:
    terminal_payload = _load_json_file(
        rebuild_root / _COGNITIVE_OVERLAY_REVISION_RELATIVE,
        "terminal_cognitive_overlay_revision_missing_or_invalid",
    )
    terminal_revision, terminal_nonce = _validated_cognitive_overlay_revision(
        terminal_payload,
        code="terminal_cognitive_overlay_revision",
    )
    if baseline_payload is None:
        _require(
            expect_advance and terminal_revision == 1,
            "cognitive_overlay_revision_initial_transition_invalid",
            str(terminal_revision),
        )
        return

    baseline_revision, baseline_nonce = _validated_cognitive_overlay_revision(
        baseline_payload,
        code="baseline_cognitive_overlay_revision",
    )
    if expect_advance:
        _require(
            terminal_revision == baseline_revision + 1
            and terminal_nonce != baseline_nonce,
            "cognitive_overlay_revision_advance_invalid",
            f"{baseline_revision}->{terminal_revision}",
        )
        return
    _require(
        dict(terminal_payload) == dict(baseline_payload),
        "frozen_cognitive_overlay_revision_changed",
    )


def _assert_rebaseline_audit_transition(
    *,
    rebuild_root: Path,
    baseline_payload: Mapping[str, Any] | None,
    board_id: str,
    manifest_ref: str,
    run_id: str,
    expected_evidence: Mapping[str, Any],
) -> None:
    """Require one run-bound, idempotent, governed legacy rebaseline record."""

    terminal = _load_json_file(
        rebuild_root / _REBASELINE_AUDIT_RELATIVE,
        "terminal_rebaseline_audit_missing_or_invalid",
    )
    _require(
        set(terminal) == {"board_id", "artifact_id", "updated_at", "records"}
        and terminal.get("artifact_id") == "records"
        and isinstance(terminal.get("records"), list),
        "terminal_rebaseline_audit_shape_invalid",
    )
    terminal_records = list(terminal["records"])
    baseline_records: list[Any] = []
    if baseline_payload is not None:
        _require(
            set(baseline_payload)
            == {"board_id", "artifact_id", "updated_at", "records"}
            and baseline_payload.get("artifact_id") == "records"
            and isinstance(baseline_payload.get("records"), list),
            "baseline_rebaseline_audit_shape_invalid",
        )
        baseline_records = list(baseline_payload["records"])
    evidence_id = f"{run_id}:{manifest_ref}"

    def is_exact_record(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        expected = {
            "board_id": board_id,
            "manifest_ref": manifest_ref,
            "evidence_id": evidence_id,
            **dict(expected_evidence),
        }
        observed = {key: item for key, item in value.items() if key != "recorded_at"}
        if observed != expected:
            return False
        try:
            _parse_utc_timestamp(
                value.get("recorded_at"),
                code="rebaseline_audit_recorded_at_invalid",
            )
        except RecoveryRefused:
            return False
        return True

    baseline_matches = tuple(item for item in baseline_records if is_exact_record(item))
    terminal_matches = tuple(item for item in terminal_records if is_exact_record(item))
    _require(
        len(baseline_matches) <= 1 and len(terminal_matches) == 1,
        "rebaseline_audit_evidence_cardinality_invalid",
    )
    if baseline_matches:
        _require(
            dict(terminal) == dict(baseline_payload or {}),
            "rebaseline_audit_idempotent_replay_changed",
        )
        return
    _require(
        terminal_records[:-1] == baseline_records
        and terminal_records[-1] == terminal_matches[0]
        and terminal.get("board_id") == board_id
        and terminal.get("updated_at") == terminal_matches[0].get("recorded_at"),
        "rebaseline_audit_append_invalid",
    )


def _assert_rebuild_artifacts_governed(
    *,
    rebuild_root: Path,
    baseline: Mapping[str, str],
    board_id: str,
    actor_id: str,
    manifest_ref: str,
    run_id: str,
    generation_id: str,
    report_id: str,
    report_ref: str,
    preflight_hash: str,
    confirmation_ref: str,
    confirmation_receipt_ref: str,
    resume_mode: bool,
    overlay_revision_baseline: Mapping[str, Any] | None,
    expect_overlay_revision_advance: bool,
    expected_cognitive_source_rows: Sequence[Mapping[str, Any]],
    cognitive_ledger_baseline: CognitiveLedgerBaseline,
    frozen_confirmation_receipt_baseline: Mapping[str, Any] | None,
    rebaseline_audit_baseline: Mapping[str, Any] | None = None,
    expected_rebaseline_evidence: Mapping[str, Any] | None = None,
) -> None:
    """Preserve old audit evidence and admit only artifacts linked to this run."""

    from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects

    f06_run_id = f"f06:{manifest_ref}"
    checkpoint_id = CommunityRebuildEffects._checkpoint_id(f06_run_id)
    checkpoint_relative = f"audit/{checkpoint_id}.json"
    enqueue_effect_id = CommunityRebuildEffects._effect_id(f"{f06_run_id}:enqueue")
    enqueue_relative = f"audit/{enqueue_effect_id}.json"
    mutable_current = f"generations/{board_id}/current.json"
    event_id = (
        "evt_"
        + hashlib.sha256(f"{board_id}\x1f{run_id}".encode("utf-8")).hexdigest()[:32]
    )
    event_relative = f"audit/events/{board_id}/{event_id}.json"
    cognitive_relative = f"audit/cognitive_pending/{board_id}/{generation_id}.json"
    active_receipt_relative = f"audit/confirmation_receipts/{board_id}/active.json"
    mutable_resume_paths: set[str] = set()
    if resume_mode:
        mutable_resume_paths.update(
            {
                checkpoint_relative,
                enqueue_relative,
                f"audit/{run_id}.json",
                f"reports/{report_id}.json",
                f"generations/{board_id}/history/{generation_id}.json",
                cognitive_relative,
                event_relative,
                f"audit/confirmation_receipts/{board_id}/{run_id}.json",
            }
        )
        if expected_rebaseline_evidence is not None:
            mutable_resume_paths.add(_REBASELINE_AUDIT_RELATIVE)
    for relative, expected_hash in baseline.items():
        if (
            relative
            in {
                mutable_current,
                _COGNITIVE_OVERLAY_REVISION_RELATIVE,
                active_receipt_relative,
            }
            or relative in mutable_resume_paths
        ):
            continue
        path = rebuild_root / relative
        _require(path.is_file(), "preexisting_rebuild_artifact_missing", relative)
        _require(
            hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash,
            "preexisting_rebuild_artifact_changed",
            relative,
        )

    expected_run_audit_ids = {
        checkpoint_id,
        *(
            CommunityRebuildEffects._effect_id(f"{f06_run_id}:{effect}")
            for effect in ("snapshot", "quarantine", "enqueue", "restore", "promote")
        ),
        CommunityRebuildEffects._effect_id(f"{f06_run_id}:audit:completed"),
    }
    after = _snapshot_tree_hashes(rebuild_root)
    if not expect_overlay_revision_advance:
        frozen_mutable_receipts = {
            active_receipt_relative,
            f"audit/confirmation_receipts/{board_id}/{run_id}.json",
        }
        changed_paths = sorted(
            relative
            for relative in set(after) | set(baseline)
            if after.get(relative) != baseline.get(relative)
            and relative not in frozen_mutable_receipts
        )
        _require(
            not changed_paths,
            "frozen_terminal_rebuild_tree_changed",
            repr(changed_paths),
        )
    cognitive_changed = after.get(cognitive_relative) != baseline.get(
        cognitive_relative
    )
    _require(
        cognitive_changed == expect_overlay_revision_advance,
        "cognitive_ledger_transition_does_not_match_terminal_mode",
        (
            "expected=changed"
            if expect_overlay_revision_advance
            else "expected=unchanged"
        ),
    )
    if not expect_overlay_revision_advance:
        _require(
            cognitive_relative in baseline
            and event_relative in baseline
            and after.get(event_relative) == baseline.get(event_relative),
            "frozen_terminal_event_or_ledger_baseline_invalid",
        )
    _assert_cognitive_overlay_revision_transition(
        rebuild_root=rebuild_root,
        baseline_payload=overlay_revision_baseline,
        expect_advance=expect_overlay_revision_advance,
    )
    if expected_rebaseline_evidence is not None:
        _assert_rebaseline_audit_transition(
            rebuild_root=rebuild_root,
            baseline_payload=rebaseline_audit_baseline,
            board_id=board_id,
            manifest_ref=manifest_ref,
            run_id=run_id,
            expected_evidence=expected_rebaseline_evidence,
        )
    receipt_path = Path(confirmation_receipt_ref).resolve(strict=False)
    try:
        receipt_relative = receipt_path.relative_to(rebuild_root.resolve()).as_posix()
    except ValueError as exc:
        raise RecoveryRefused("confirmation_receipt_escaped_rebuild_root") from exc
    allowed_receipt_paths = {
        active_receipt_relative,
        f"audit/confirmation_receipts/{board_id}/{run_id}.json",
    }
    _require(
        receipt_relative == f"audit/confirmation_receipts/{board_id}/{run_id}.json",
        "confirmation_receipt_reference_not_exact_run",
        receipt_relative,
    )
    _require(
        allowed_receipt_paths <= set(after),
        "confirmation_receipt_artifact_set_incomplete",
        repr(sorted(allowed_receipt_paths - set(after))),
    )
    active_receipt_payload = _load_json_file(
        rebuild_root / active_receipt_relative,
        "active_confirmation_receipt_invalid",
    )
    history_receipt_payload = _load_json_file(
        rebuild_root / receipt_relative,
        "history_confirmation_receipt_invalid",
    )
    _require(
        active_receipt_payload == history_receipt_payload
        and str(active_receipt_payload.get("run_id")) == run_id
        and str(active_receipt_payload.get("board_id")) == board_id
        and str(active_receipt_payload.get("receipt_state")) == "terminal",
        "terminal_confirmation_receipt_journal_conflict",
        run_id,
    )
    if not expect_overlay_revision_advance:
        _require(
            frozen_confirmation_receipt_baseline is not None
            and frozen_confirmation_receipt_baseline.get("receipt_state")
            in (None, "authorized"),
            "frozen_terminal_confirmation_baseline_invalid",
            run_id,
        )
        assert frozen_confirmation_receipt_baseline is not None
        expected_archived_receipt = {
            **dict(frozen_confirmation_receipt_baseline),
            "receipt_state": "terminal",
        }
        _require(
            active_receipt_payload == expected_archived_receipt,
            "frozen_terminal_confirmation_archive_transition_invalid",
            run_id,
        )
    new_paths = sorted(set(after) - set(baseline))
    expected_f06_paths = {
        f"audit/{artifact_id}.json" for artifact_id in expected_run_audit_ids
    }
    required_paths = {
        f"manifests/{manifest_ref}.json",
        f"reports/{report_id}.json",
        f"generations/{board_id}/current.json",
        f"generations/{board_id}/history/{generation_id}.json",
        f"audit/{run_id}.json",
        event_relative,
        cognitive_relative,
        _COGNITIVE_OVERLAY_REVISION_RELATIVE,
        *allowed_receipt_paths,
        *expected_f06_paths,
    }
    if expected_rebaseline_evidence is not None:
        required_paths.add(_REBASELINE_AUDIT_RELATIVE)
    _require(
        required_paths <= set(after),
        "terminal_rebuild_artifact_set_incomplete",
        repr(sorted(required_paths - set(after))),
    )

    confirmation_prefix = f"audit/confirmation/{board_id}/"
    new_confirmation_paths = tuple(
        relative for relative in new_paths if relative.startswith(confirmation_prefix)
    )
    _require(
        len(new_confirmation_paths) == 0
        if resume_mode
        else len(new_confirmation_paths) <= 1,
        "confirmation_consumption_audit_new_cardinality_invalid",
        repr(new_confirmation_paths),
    )
    confirmation_name = re.compile(
        rf"^{re.escape(confirmation_prefix)}audit_[0-9a-f]{{32}}\.json$"
    )
    expected_actor_type = (
        "agent"
        if actor_id.lower().startswith("agent-")
        or "agent" in actor_id.lower().split("-")
        else ("human" if actor_id else "unknown")
    )
    matching_confirmation_paths: list[str] = []
    for relative in sorted(
        path for path in after if path.startswith(confirmation_prefix)
    ):
        if confirmation_name.fullmatch(relative) is None:
            continue
        payload = _load_json_file(
            rebuild_root / relative,
            "confirmation_consumption_audit_invalid",
        )
        generation_ids = payload.get("generation_ids")
        exact_binding = all(
            (
                str(payload.get("audit_id")) == Path(relative).stem,
                str(payload.get("board_id")) == board_id,
                str(payload.get("operation")) == OPERATION,
                str(payload.get("outcome")) == "consumed",
                str(payload.get("reason")) == "n/a",
                str(payload.get("preflight_hash")) == preflight_hash,
                str(payload.get("confirmation_ref")) == confirmation_ref,
                str(payload.get("actor_ref")) == f"actor:{expected_actor_type}",
                isinstance(generation_ids, Mapping),
                str(dict(generation_ids or {}).get("manifest_ref")) == manifest_ref,
            )
        )
        if str(payload.get("confirmation_ref")) == confirmation_ref:
            _require(
                exact_binding,
                "confirmation_consumption_audit_scope_conflict",
                relative,
            )
        if exact_binding:
            matching_confirmation_paths.append(relative)
    _require(
        len(matching_confirmation_paths) <= 1,
        "confirmation_consumption_audit_binding_invalid",
        repr(matching_confirmation_paths),
    )
    if new_confirmation_paths:
        _require(
            tuple(matching_confirmation_paths) == new_confirmation_paths,
            "new_confirmation_consumption_audit_binding_invalid",
        )

    terminal_event_payload = _load_json_file(
        rebuild_root / event_relative,
        "terminal_rebuild_event_audit_invalid",
    )
    _require(
        all(
            (
                str(terminal_event_payload.get("event_id")) == event_id,
                str(terminal_event_payload.get("event")) == "kg.rebuilt",
                str(terminal_event_payload.get("board_id")) == board_id,
                str(terminal_event_payload.get("run_id")) == run_id,
                str(terminal_event_payload.get("manifest_ref")) == manifest_ref,
                str(terminal_event_payload.get("report_ref")) == report_ref,
                str(terminal_event_payload.get("kg_generation_id")) == generation_id,
                str(terminal_event_payload.get("status")) == "completed",
                str(terminal_event_payload.get("delivery_outcome")) == "published",
            )
        ),
        "terminal_rebuild_event_audit_binding_invalid",
    )
    event_rebaseline_evidence_id = terminal_event_payload.get("rebaseline_evidence_id")
    event_rebaseline_target_hash = terminal_event_payload.get(
        "rebaseline_target_source_set_hash"
    )
    if expected_rebaseline_evidence is None:
        _require(
            event_rebaseline_evidence_id is None
            and event_rebaseline_target_hash is None,
            "terminal_rebuild_event_has_unproved_rebaseline_binding",
        )
    else:
        expected_rebaseline_target_hash = str(
            expected_rebaseline_evidence.get("to_source_set_hash") or ""
        )
        _require(
            re.fullmatch(r"[0-9a-f]{64}", expected_rebaseline_target_hash) is not None,
            "expected_rebaseline_target_source_set_hash_invalid",
        )
        _require(
            event_rebaseline_evidence_id == f"{run_id}:{manifest_ref}"
            and event_rebaseline_target_hash == expected_rebaseline_target_hash,
            "terminal_rebuild_event_rebaseline_binding_invalid",
        )
    terminal_cognitive_payload = _load_json_file(
        rebuild_root / cognitive_relative,
        "terminal_cognitive_pending_invalid",
    )
    from okto_pulse.core.kg.rebuild_audit import (
        ACTIVE_ITEM_STATUSES,
        CONSOLIDABLE_ARTIFACT_TYPES,
        CognitiveConsolidationItem,
        compute_cognitive_item_id,
    )

    expected_cognitive: dict[str, Mapping[str, Any]] = {}
    for source_row in expected_cognitive_source_rows:
        artifact_type = str(source_row.get("artifact_type") or "")
        if artifact_type not in CONSOLIDABLE_ARTIFACT_TYPES:
            continue
        source_ref = str(source_row.get("source_ref", source_row.get("id", "")))
        _require(bool(source_ref), "expected_cognitive_source_ref_missing")
        _require(
            source_ref not in expected_cognitive,
            "expected_cognitive_source_ref_duplicate",
            source_ref,
        )
        expected_cognitive[source_ref] = source_row
    frozen_items: dict[str, dict[str, Any]] | None = None
    if expect_overlay_revision_advance:
        carry_forward, unrelated_same_generation = _select_cognitive_carry_forward_rows(
            cognitive_ledger_baseline,
            target_generation_id=generation_id,
            expected_by_source=expected_cognitive,
        )
    else:
        baseline_records = {
            record_generation: record
            for record_generation, record in cognitive_ledger_baseline.records
        }
        frozen_record = baseline_records.get(generation_id)
        _require(
            frozen_record is not None
            and dict(terminal_cognitive_payload) == dict(frozen_record),
            "frozen_terminal_cognitive_record_changed",
            generation_id,
        )
        assert frozen_record is not None
        frozen_raw_items = frozen_record.get("items")
        _require(
            isinstance(frozen_raw_items, list),
            "frozen_terminal_cognitive_items_invalid",
        )
        assert isinstance(frozen_raw_items, list)
        frozen_items = {}
        allowed_statuses = {
            "pending",
            "in_progress",
            "consolidated",
            "skipped",
            "failed",
        }
        for raw_item in frozen_raw_items:
            assert isinstance(raw_item, Mapping)  # capture validated this
            normalized = CognitiveConsolidationItem.from_dict(raw_item).to_dict()
            source_ref = str(normalized["source_ref"])
            _require(
                dict(raw_item) == normalized
                and str(normalized.get("status") or "") in allowed_statuses
                and source_ref not in frozen_items,
                "frozen_terminal_cognitive_item_invalid",
                source_ref,
            )
            frozen_items[source_ref] = normalized
        _require(
            set(expected_cognitive) <= set(frozen_items),
            "frozen_terminal_cognitive_source_set_incomplete",
        )
        carry_forward = {}
        unrelated_same_generation = {
            source_ref: row
            for source_ref, row in frozen_items.items()
            if source_ref not in expected_cognitive
        }
    raw_items = terminal_cognitive_payload.get("items")
    _require(isinstance(raw_items, list), "terminal_cognitive_items_invalid")
    assert isinstance(raw_items, list)
    ledger_recorded_at = str(terminal_cognitive_payload.get("recorded_at") or "")
    _require(bool(ledger_recorded_at), "terminal_cognitive_recorded_at_missing")
    actual_cognitive: dict[str, Mapping[str, Any]] = {}
    active_refs: list[str] = []
    for raw_item in raw_items:
        _require(isinstance(raw_item, Mapping), "terminal_cognitive_item_invalid")
        assert isinstance(raw_item, Mapping)
        source_ref = str(raw_item.get("source_ref") or "")
        _require(
            bool(source_ref) and source_ref not in actual_cognitive,
            "terminal_cognitive_source_ref_invalid",
            source_ref,
        )
        actual_cognitive[source_ref] = raw_item
        expected_row = expected_cognitive.get(source_ref)
        if frozen_items is not None:
            expected_item = frozen_items.get(source_ref)
            _require(
                expected_item is not None,
                "frozen_terminal_cognitive_source_ref_unexpected",
                source_ref,
            )
            if expected_row is not None:
                _require(
                    str(raw_item.get("artifact_type") or "")
                    == str(expected_row.get("artifact_type") or ""),
                    "frozen_terminal_cognitive_source_binding_invalid",
                    source_ref,
                )
        elif expected_row is None:
            expected_item = unrelated_same_generation.get(source_ref)
            _require(
                expected_item is not None,
                "terminal_cognitive_source_ref_unexpected",
                source_ref,
            )
        elif source_ref in carry_forward:
            expected_item = carry_forward[source_ref]
        else:
            expected_hash = expected_row.get("content_hash")
            expected_item = CognitiveConsolidationItem(
                item_id=compute_cognitive_item_id(
                    board_id,
                    generation_id,
                    source_ref,
                ),
                board_id=board_id,
                kg_generation_id=generation_id,
                source_ref=source_ref,
                artifact_type=str(expected_row.get("artifact_type") or ""),
                status="pending",
                recorded_at=ledger_recorded_at,
                event_ref=event_id,
                content_hash=(str(expected_hash) if expected_hash else None),
            ).to_dict()
        _require(
            dict(raw_item) == dict(expected_item),
            "terminal_cognitive_item_binding_invalid",
            source_ref,
        )
        if str(raw_item.get("status")) in ACTIVE_ITEM_STATUSES:
            active_refs.append(source_ref)
    _require(
        (
            set(actual_cognitive) == set(frozen_items)
            if frozen_items is not None
            else set(actual_cognitive)
            == set(expected_cognitive) | set(unrelated_same_generation)
        ),
        "terminal_cognitive_source_set_mismatch",
    )
    pending_refs = terminal_cognitive_payload.get("pending_refs")
    _require(
        all(
            (
                str(terminal_cognitive_payload.get("board_id")) == board_id,
                str(terminal_cognitive_payload.get("kg_generation_id"))
                == generation_id,
                str(terminal_cognitive_payload.get("event_ref")) == event_id,
                str(terminal_cognitive_payload.get("status"))
                == ("pending_marked" if expected_cognitive else "skipped"),
                type(terminal_cognitive_payload.get("pending_count")) is int,
                int(terminal_cognitive_payload.get("pending_count"))
                == len(active_refs),
                isinstance(pending_refs, list),
                tuple(str(value) for value in pending_refs)
                == tuple(sorted(active_refs)),
            )
        ),
        "terminal_cognitive_pending_binding_invalid",
    )

    identities = frozenset(
        {
            manifest_ref,
            run_id,
            generation_id,
            report_id,
            report_ref,
            f06_run_id,
            preflight_hash,
            confirmation_ref,
        }
    )
    for relative in new_paths:
        _require(
            not relative.endswith(".tmp"),
            "transient_rebuild_artifact_present",
            relative,
        )
        _require(
            not relative.startswith("confirmations/"),
            "confirmation_token_not_consumed",
            relative,
        )
        path = rebuild_root / relative
        _require(path.suffix == ".json", "unexpected_rebuild_artifact", relative)
        identity_binding_required = True
        if relative.startswith("manifests/"):
            _require(
                relative == f"manifests/{manifest_ref}.json",
                "unexpected_manifest_artifact",
                relative,
            )
        elif relative.startswith("reports/"):
            _require(
                relative == f"reports/{report_id}.json",
                "unexpected_report_artifact",
                relative,
            )
        elif relative.startswith("generations/"):
            _require(
                relative
                in {
                    f"generations/{board_id}/current.json",
                    f"generations/{board_id}/history/{generation_id}.json",
                },
                "unexpected_generation_artifact",
                relative,
            )
        elif relative == _COGNITIVE_OVERLAY_REVISION_RELATIVE:
            identity_binding_required = False
        elif relative == _REBASELINE_AUDIT_RELATIVE:
            _require(
                expected_rebaseline_evidence is not None,
                "unexpected_rebaseline_audit_artifact",
                relative,
            )
        elif relative.startswith("audit/confirmation_receipts/"):
            _require(
                relative in allowed_receipt_paths,
                "unexpected_confirmation_receipt_artifact",
                relative,
            )
        elif relative.startswith("audit/confirmation/"):
            _require(
                relative in matching_confirmation_paths,
                "unexpected_confirmation_consumption_audit",
                relative,
            )
        elif relative.startswith("audit/events/"):
            _require(
                relative == event_relative,
                "unexpected_rebuild_event_audit",
                relative,
            )
        elif relative.startswith("audit/cognitive_pending/"):
            _require(
                relative == cognitive_relative,
                "unexpected_cognitive_pending_artifact",
                relative,
            )
        elif relative.startswith("audit/f06-"):
            _require(
                relative in expected_f06_paths,
                "unexpected_run_audit_artifact",
                relative,
            )
        elif relative.startswith("audit/") and relative.count("/") == 1:
            _require(
                relative == f"audit/{run_id}.json",
                "unexpected_top_level_run_audit",
                relative,
            )
        else:
            raise RecoveryRefused(f"unexpected_rebuild_namespace:{relative}")
        payload = _load_json_file(path, "new_rebuild_artifact_invalid")
        if identity_binding_required:
            _require(
                _payload_mentions_any(payload, identities),
                "new_rebuild_artifact_not_linked_to_run",
                relative,
            )


class ServeLockHeartbeat:
    """Refresh the serve-lock on a real OS thread and expose a lifetime probe."""

    def __init__(self, lock: Any, *, interval_seconds: float) -> None:
        self._lock = lock
        self._interval = max(1.0, float(interval_seconds))
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="pulse-recovery-serve-lock-heartbeat",
            daemon=False,
        )

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._lock.refresh_heartbeat()
            except BaseException as exc:  # lifetime fence, never masked
                self._failure = exc
                self._stop.set()
                return

    def stop(self) -> None:
        self._stop.set()
        while self._thread.ident is not None and self._thread.is_alive():
            self._thread.join(timeout=30.0)
            if self._thread.is_alive():
                _emit(
                    "serve_lock_heartbeat_join_watchdog",
                    teardown_deferred=True,
                )


def _authoritative_serve_lock_matches(
    serve_lock: Any,
    *,
    data_home: Path,
) -> bool:
    """Prove the pathname still names this exact fresh lock, fail closed."""

    try:
        from okto_pulse.community.serve_lock import (
            get_active_lock,
            inspect_serve_lock_identity,
        )

        if get_active_lock() is not serve_lock:
            return False
        if not bool(getattr(serve_lock, "is_acquired", False)):
            return False
        if Path(str(getattr(serve_lock, "data_dir", ""))).resolve() != data_home:
            return False
        lock_path = Path(str(getattr(serve_lock, "lock_path", "")))
        if lock_path.resolve() != (data_home / lock_path.name).resolve():
            return False
        fd = getattr(serve_lock, "_fd", None)
        if not isinstance(fd, int) or fd < 0 or not lock_path.is_file():
            return False
        fd_stat = os.fstat(fd)
        path_stat = lock_path.stat()
        if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            return False
        identity = inspect_serve_lock_identity(serve_lock)
        return bool(
            identity.get("state") == "confirmed"
            and str(identity.get("instance_id") or "")
            == str(getattr(serve_lock, "instance_id", ""))
            and int(identity.get("pid") or 0) == os.getpid()
            and Path(str(identity.get("data_dir") or "")).resolve() == data_home
        )
    except BaseException:
        return False


def _assert_settings_identity(settings: Any, data_home: Path, db_path: Path) -> None:
    actual_home = Path(str(settings.data_dir)).resolve()
    actual_kg = Path(str(settings.kg_base_dir)).resolve()
    _require(actual_home == data_home, "composed_data_home_mismatch", str(actual_home))
    _require(actual_kg == data_home, "composed_kg_home_mismatch", str(actual_kg))
    expected_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    _require(
        str(settings.database_url) == expected_url,
        "composed_database_url_mismatch",
        str(settings.database_url),
    )
    for field in ("upload_dir", "metrics_dir"):
        resolved = Path(str(getattr(settings, field))).resolve()
        _require(
            resolved == data_home or data_home in resolved.parents,
            "composed_path_escaped_data_home",
            f"{field}={resolved}",
        )


def _assert_worker_registry_never_started(composition: Any) -> None:
    registry = composition.worker_registry
    _require(registry is not None, "worker_registry_missing")
    _require(
        tuple(registry.active_families) == (),
        "worker_registry_active",
        repr(tuple(registry.active_families)),
    )
    started = {
        family: int(registry.start_count(family))
        for family in tuple(registry.families)
        if int(registry.start_count(family)) != 0
    }
    _require(not started, "worker_registry_was_started", repr(started))


async def _authorize_governed_rebuild(
    composition: Any,
    *,
    board_id: str,
    actor_id: str,
) -> None:
    from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
    from okto_pulse.core.application.use_cases.authorize_operation import (
        AuthorizeOperationCommand,
        AuthorizeOperationUseCase,
    )
    from okto_pulse.core.application.use_cases.board_access import load_accessible_board

    actor = RESTAdapterContract.actor(actor_id, board_id=board_id)
    async with composition.uow_factory(actor=actor) as uow:
        board = await load_accessible_board(uow, board_id, actor)
        _require(board is not None, "governed_board_access_refused")
        for operation, legacy in (
            ("kg.operations.rebuild.preflight", "kg.admin.settings_read"),
            ("kg.operations.rebuild.confirm", "kg.admin.settings_write"),
            ("kg.operations.rebuild.run", "kg.admin.settings_write"),
        ):
            await AuthorizeOperationUseCase().execute(
                AuthorizeOperationCommand(
                    operation,
                    legacy_operation=legacy,
                    board_id=board_id,
                ),
                actor=actor,
                uow=uow,
            )


async def _real_health(composition: Any, board_id: str) -> dict[str, Any]:
    from okto_pulse.community.api.kg_health_probe import get_kg_health

    async with composition.uow_factory() as uow:
        return dict(
            await get_kg_health(
                board_id,
                uow,
                scheduler_control=composition.scheduler_control,
            )
        )


def _offline_cold_graph_health(
    bundle: ServiceBundle,
    *,
    board_id: str,
    board_storage_root: Path,
    board_storage_snapshot: Mapping[str, str],
) -> dict[str, Any]:
    """Build only the health fields needed before admission without opening Ladybug.

    Opening an embedded Ladybug database is not physically read-only: on Windows
    it both retains an exclusive cached ``Database`` handle and may normalize
    native pages.  Preflight therefore consumes a conservative cold projection.
    It never claims the graph is healthy; terminal queryability is proved later,
    under the governed recovery capability, by :func:`_real_health`.
    """

    from okto_pulse.community.adapters.kuzu_graph_path_resolver import (
        CommunityKuzuGraphPathResolver,
    )

    state = CommunityKuzuGraphPathResolver().storage_state(board_id)
    expected_path = board_storage_root / "graph.lbug"
    _require(
        state.path.resolve(strict=False) == expected_path.resolve(strict=False),
        "cold_graph_storage_path_mismatch",
    )
    graph_present = "graph.lbug" in board_storage_snapshot
    _require(
        bool(state.exists) == graph_present,
        "cold_graph_storage_snapshot_drift",
        board_id,
    )
    snapshot_sidecars = {
        relative
        for relative in board_storage_snapshot
        if "/" not in relative
        and relative != "graph.lbug"
        and relative.startswith("graph.lbug")
    }
    _require(
        set(state.sidecars) == snapshot_sidecars,
        "cold_graph_sidecar_snapshot_drift",
        board_id,
    )
    return {
        # ``recovery_needed`` is deliberately conservative: no native graph
        # query has run, so this projection must never manufacture ``healthy``.
        "graph_state": "quarantined" if state.quarantined else "recovery_needed",
        "metric_status": "unavailable",
        "current_kg_generation_id": bundle.generation_repository.get_current(board_id),
        "graph_storage_exists": bool(state.exists),
        "graph_storage_locked": bool(state.locked),
    }


def _snapshot_closed_board_storage(
    *,
    board_id: str,
    board_storage_root: Path,
    phase: str,
    drain_timeout_seconds: float = 30.0,
) -> dict[str, str]:
    """Drain readers, close the native DB cache, and hash inside that fence."""

    from okto_pulse.community.adapters.kg_runtime import (
        board_storage_mutation_window,
    )

    try:
        with board_storage_mutation_window(
            board_id,
            phase=f"recovery_snapshot:{phase}",
            drain_timeout=drain_timeout_seconds,
        ):
            snapshot = _snapshot_tree_hashes(board_storage_root)
    except RecoveryRefused:
        raise
    except BaseException as exc:
        raise RecoveryRefused(
            f"board_graph_close_before_snapshot_failed:phase={phase!r};"
            f"type={type(exc).__name__}"
        ) from exc
    _emit(
        "board_graph_closed_snapshot_captured",
        board_id=board_id,
        phase=phase,
        file_count=len(snapshot),
    )
    return snapshot


def _resolve_event_cognitive_sources(
    *,
    binding: EventManifestBinding,
    event_payload: Mapping[str, Any],
    manifest_store: Any,
    source_enumerator: Any,
) -> tuple[dict[str, Any], ...]:
    """Resolve the exact terminal-marker denominator from a proved plan cut."""

    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    manifest_ref = str(event_payload.get("manifest_ref") or "")
    if str(event_payload.get("board_id") or "") != binding.board_id:
        raise RuntimeError("recovery_event_manifest_board_mismatch")
    current_source_set = source_enumerator.enumerate(board_id=binding.board_id)
    manifest = manifest_store.load_verified(
        manifest_ref,
        expected_board_id=binding.board_id,
        expected_preflight_hash=binding.preflight_hash,
        cognitive_digest=current_source_set.cognitive_durable_digest,
    )
    revalidation = manifest_store.classify_revalidation(
        manifest=manifest,
        current_source_set=current_source_set,
    )
    if binding.rebaseline_evidence_id is None:
        if any(
            event_payload.get(key) not in (None, "")
            for key in (
                "rebaseline_evidence_id",
                "rebaseline_target_source_set_hash",
            )
        ):
            raise RuntimeError("recovery_event_rebaseline_binding_unexpected")
        if revalidation.outcome is not SourceSetRevalidation.EQUIVALENT:
            raise RuntimeError("recovery_event_source_drift")
        return tuple(row.to_dict() for row in manifest.materializable_sources)

    expected_evidence_id = f"{event_payload.get('run_id')}:{manifest_ref}"
    if (
        binding.rebaseline_evidence_id != expected_evidence_id
        or event_payload.get("rebaseline_evidence_id") != binding.rebaseline_evidence_id
        or event_payload.get("rebaseline_target_source_set_hash")
        != binding.rebaseline_target_source_set_hash
    ):
        raise RuntimeError("recovery_event_rebaseline_binding_mismatch")
    if (
        revalidation.outcome is not SourceSetRevalidation.REBASELINE
        or revalidation.to_source_set_hash != binding.rebaseline_target_source_set_hash
    ):
        raise RuntimeError("recovery_event_rebaseline_source_drift")
    live_rows = tuple(
        row.to_dict() for row in current_source_set.materializable_sources
    )
    if _canonical_source_rows(live_rows) != _canonical_source_rows(
        binding.rebaseline_materializable_sources or ()
    ):
        raise RuntimeError("recovery_event_rebaseline_projection_drift")
    return live_rows


def _build_service_bundle(
    *,
    legacy_evidence_probe: Callable[[Any], bool] | None = None,
    legacy_db_path: Path | None = None,
) -> ServiceBundle:
    from okto_pulse.core.application.kg_rebuild import (
        build_rebuild_step_adapter,
        build_source_store,
    )
    from okto_pulse.core.application.kg_runtime_access import (
        require_rebuild_audit_artifact_store,
        resolve_graph_lifecycle,
    )
    from okto_pulse.core.kg.orphan_integrity import OrphanNodeScanner
    from okto_pulse.core.kg.rebuild_audit import (
        CognitivePendingMarker,
        ConfirmationConsumptionAuditRecorder,
        KGRebuiltEventPublisher,
        build_kg_rebuilt_event_handler,
    )
    from okto_pulse.core.kg.rebuild_confirmation import RebuildConfirmationStore
    from okto_pulse.core.kg.rebuild_generation import (
        KGGenerationPromotionGuard,
        RebuildAuditKGGenerationRepository,
    )
    from okto_pulse.core.kg.rebuild_report import (
        RebuildReportStore,
        RebuildReportTerminalStateGuard,
    )
    from okto_pulse.core.kg.rebuild_service import KGRebuildService
    from okto_pulse.core.kg.rebuild_sources import (
        KGRebuildSourceManifest,
        RebuildSourceEnumerator,
    )
    from okto_pulse.core.kg.safe_write_lifecycle import (
        HealthProbe,
        KGSafeWriteLifecycle,
        LockOwnerProbe,
    )
    from okto_pulse.core.kg.single_writer_lock import (
        KGAdministrativeOperationReservation,
        KGSingleWriterLock,
    )

    artifact_store = require_rebuild_audit_artifact_store()
    manifest_store = KGRebuildSourceManifest(artifact_store=artifact_store)
    source_enumerator = RebuildSourceEnumerator(source_store=build_source_store())
    single_writer_lock = KGSingleWriterLock()
    operation_reservation = KGAdministrativeOperationReservation()

    def is_owner(board_id: str, owner_token: str) -> bool:
        manifest = single_writer_lock.inspect(board_id=board_id)
        return manifest is not None and manifest.owner_token == owner_token

    safe_lifecycle = KGSafeWriteLifecycle(
        step_adapter=resolve_graph_lifecycle().apply_step,
        owner_probe=LockOwnerProbe(is_active_owner=is_owner),
        health_probe=HealthProbe(classify=lambda _b, _g, _s, _step: "at_risk"),
    )
    audit_recorder = ConfirmationConsumptionAuditRecorder(artifact_store=artifact_store)
    confirmation_store = RebuildConfirmationStore(
        audit_recorder=audit_recorder,
        artifact_store=artifact_store,
    )
    generation_repository = RebuildAuditKGGenerationRepository(
        artifact_store=artifact_store
    )
    report_store = RebuildReportStore(artifact_store=artifact_store)
    orphan_scanner = OrphanNodeScanner()
    event_manifest_bindings: dict[str, EventManifestBinding] = {}

    def source_resolver(event_payload: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        manifest_ref = str(event_payload.get("manifest_ref") or "")
        binding = event_manifest_bindings.get(manifest_ref)
        if binding is None:
            raise RuntimeError("recovery_event_manifest_not_authorized")
        return _resolve_event_cognitive_sources(
            binding=binding,
            event_payload=event_payload,
            manifest_store=manifest_store,
            source_enumerator=source_enumerator,
        )

    event_handler = build_kg_rebuilt_event_handler(
        publisher=KGRebuiltEventPublisher(artifact_store=artifact_store),
        cognitive_marker=CognitivePendingMarker(artifact_store=artifact_store),
        source_resolver=source_resolver,
    )
    legacy_adapter: Callable[[Any], Any] | None = None
    if legacy_evidence_probe is not None:
        from okto_pulse.community.adapters.board_rebuild_ingestion import (
            CommunityBoardRebuildIngestionAdapter,
        )

        _require(
            isinstance(legacy_db_path, Path),
            "legacy_manual_restore_queue_only_database_missing",
        )
        ingestion = CommunityBoardRebuildIngestionAdapter(
            db_path=legacy_db_path,
            artifact_store=artifact_store,
        )
        legacy_adapter = ingestion.build_legacy_manual_restore_queue_only_adapter(
            evidence_probe=legacy_evidence_probe
        )

    service = KGRebuildService(
        base_dir=None,
        single_writer_lock=single_writer_lock,
        safe_write_lifecycle=safe_lifecycle,
        quarantine_service=None,
        confirmation_store=confirmation_store,
        manifest_store=manifest_store,
        source_enumerator=source_enumerator,
        rebuild_step_adapter=build_rebuild_step_adapter(
            manifest_store_obj=manifest_store
        ),
        generation_repository=generation_repository,
        promotion_guard=KGGenerationPromotionGuard,
        report_store=report_store,
        terminal_state_guard=RebuildReportTerminalStateGuard,
        event_emitter=event_handler,
        orphan_scan_provider=lambda board_id, generation_id: orphan_scanner.scan(
            board_id=board_id,
            generation_id=generation_id,
        ),
        operation_reservation=operation_reservation,
        artifact_store=artifact_store,
        legacy_manual_restore_queue_only_adapter=legacy_adapter,
    )
    return ServiceBundle(
        artifact_store=artifact_store,
        confirmation_store=confirmation_store,
        generation_repository=generation_repository,
        manifest_store=manifest_store,
        orphan_scanner=orphan_scanner,
        operation_reservation=operation_reservation,
        service=service,
        single_writer_lock=single_writer_lock,
        source_enumerator=source_enumerator,
        event_manifest_bindings=event_manifest_bindings,
    )


def _create_preflight_and_manifest(
    bundle: ServiceBundle,
    *,
    board_id: str,
    raw_health: Mapping[str, Any],
) -> tuple[Any, Any, Any]:
    from okto_pulse.core.kg.rebuild_preflight import (
        RebuildHealthSummary,
        RebuildPreflightService,
        RebuildSourceSummary,
    )

    source_set = bundle.source_enumerator.enumerate(board_id=board_id)

    def source_probe(_board_id: str) -> RebuildSourceSummary:
        return RebuildSourceSummary(
            eligible_count=source_set.eligible_count,
            skipped_cancelled_count=source_set.skipped_cancelled_count,
            has_non_deterministic_inputs=source_set.has_non_deterministic_inputs,
            canonical_source_count=source_set.canonical_source_count,
            working_source_count=source_set.working_source_count,
            skipped_by_maturity_count=source_set.skipped_by_maturity_count,
            skipped_expired_working_count=source_set.skipped_expired_working_count,
            legacy_unknown_count=source_set.legacy_unknown_count,
            layer_counts=source_set.layer_counts,
            source_partition_counts=source_set.source_partition_counts,
        )

    def health_probe(_board_id: str) -> RebuildHealthSummary:
        return RebuildHealthSummary(
            base_state=str(raw_health.get("graph_state") or "healthy"),
            metric_status=str(raw_health.get("metric_status") or "unavailable"),
            current_kg_generation_id=raw_health.get("current_kg_generation_id"),
        )

    preflight = RebuildPreflightService(
        source_probe=source_probe,
        health_probe=health_probe,
    ).run(board_id=board_id)
    _require(
        preflight.outcome != "blocked", "rebuild_preflight_blocked", preflight.reason
    )
    manifest = bundle.manifest_store.build(
        source_set=source_set,
        preflight_hash=preflight.preflight_hash,
    )
    _require(manifest.board_id == board_id, "manifest_board_mismatch")
    _require(
        manifest.source_set_hash,
        "manifest_source_set_hash_missing",
    )
    return preflight, manifest, source_set


def _legacy_effect_relative(effect_key: str) -> str:
    digest = hashlib.sha256(effect_key.encode("utf-8")).hexdigest()[:24]
    return f"audit/f06-effect-{digest}.json"


def _read_baseline_json(
    root: Path,
    baseline: Mapping[str, str],
    relative: str,
    *,
    code: str,
) -> dict[str, Any]:
    """Read one raw-snapshotted artifact and prove its bytes did not move."""

    _require(
        relative in baseline and _is_sha256(baseline.get(relative)),
        f"{code}_missing",
        relative,
    )
    path = root / Path(relative)
    try:
        raw = _read_bounded_stable_file(
            path,
            code=code,
            max_bytes=MAX_GOVERNED_REBUILD_ARTIFACT_BYTES,
        )
        current = hashlib.sha256(raw).hexdigest()
        payload = _strict_json_mapping(raw, code=f"{code}_invalid")
    except RecoveryRefused:
        raise
    _require(current == baseline[relative], f"{code}_changed", relative)
    return payload


def _relative_rebuild_reference(
    reference: object,
    *,
    rebuild_root: Path,
    prefix: str,
    code: str,
) -> str:
    _require(isinstance(reference, str) and bool(reference), code)
    try:
        resolved = Path(str(reference)).resolve(strict=False)
        relative = resolved.relative_to(rebuild_root.resolve(strict=False)).as_posix()
    except (OSError, ValueError) as exc:
        raise RecoveryRefused(code) from exc
    _require(
        relative.startswith(prefix)
        and relative.endswith(".json")
        and ".." not in relative.split("/"),
        code,
        relative,
    )
    return relative


def _legacy_receipt_from_payload(payload: Mapping[str, Any], *, code: str) -> Any:
    from okto_pulse.core.application.rebuild_processor import RebuildEffectReceipt

    _require(
        set(payload) == {"effect_key", "effect", "ok", "code", "details"}
        and isinstance(payload.get("effect_key"), str)
        and isinstance(payload.get("effect"), str)
        and type(payload.get("ok")) is bool
        and isinstance(payload.get("code"), str)
        and isinstance(payload.get("details"), Mapping),
        code,
    )
    return RebuildEffectReceipt(
        effect_key=str(payload["effect_key"]),
        effect=str(payload["effect"]),
        ok=bool(payload["ok"]),
        code=str(payload["code"]),
        details=_strict_json_mapping(
            _canonical_json_bytes(dict(payload["details"])),
            code=code,
        ),
    )


def _legacy_command_from_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    board_id: str,
) -> Any:
    from okto_pulse.core.application.rebuild_processor import RebuildCommand

    command = checkpoint.get("command")
    _require(isinstance(command, Mapping), "legacy_queue_only_command_missing")
    assert isinstance(command, Mapping)
    expected_keys = {
        "run_id",
        "board_id",
        "manifest_ref",
        "operation",
        "actor_id",
        "reason",
        "source_rows",
        "previous_generation_id",
        "candidate_generation_id",
        "salvage_pending",
    }
    _require(
        set(command) == expected_keys,
        "legacy_queue_only_command_shape_invalid",
    )
    manifest_ref = str(command.get("manifest_ref") or "")
    run_id = str(command.get("run_id") or "")
    candidate_generation_id = str(command.get("candidate_generation_id") or "")
    try:
        UUID(candidate_generation_id)
    except ValueError as exc:
        raise RecoveryRefused("legacy_queue_only_candidate_generation_invalid") from exc
    raw_rows = command.get("source_rows")
    _require(
        str(command.get("board_id")) == board_id
        and re.fullmatch(r"rebuild_manifest_[A-Za-z0-9_-]+", manifest_ref) is not None
        and run_id == f"f06:{manifest_ref}"
        and command.get("operation") == OPERATION
        and isinstance(command.get("actor_id"), str)
        and bool(command.get("actor_id"))
        and isinstance(command.get("reason"), str)
        and bool(command.get("reason"))
        and command.get("previous_generation_id") is None
        and command.get("salvage_pending") is False
        and isinstance(raw_rows, list)
        and bool(raw_rows),
        "legacy_queue_only_command_binding_invalid",
    )
    assert isinstance(raw_rows, list)
    source_rows = tuple(dict(row) for row in raw_rows if isinstance(row, Mapping))
    _require(
        len(source_rows) == len(raw_rows),
        "legacy_queue_only_source_rows_invalid",
    )
    _expected_queue_order(source_rows)
    return RebuildCommand(
        run_id=run_id,
        board_id=board_id,
        manifest_ref=manifest_ref,
        operation=OPERATION,
        actor_id=str(command["actor_id"]),
        reason=str(command["reason"]),
        source_rows=source_rows,
        previous_generation_id=None,
        candidate_generation_id=candidate_generation_id,
        salvage_pending=False,
    )


def _assert_legacy_command_bound_to_manifest(command: Any, manifest: Any) -> None:
    """Bind every persisted command row to the verified historical manifest."""

    from okto_pulse.community.adapters.board_rebuild_ingestion import (
        _EVIDENCE_CLOSURE_CANDIDATE,
    )

    created_at = str(getattr(manifest, "created_at", "") or "")
    _require(
        getattr(manifest, "manifest_ref", None) == command.manifest_ref
        and getattr(manifest, "board_id", None) == command.board_id
        and bool(created_at),
        "legacy_queue_only_manifest_binding_invalid",
    )

    def projected(raw: Any) -> dict[str, Any]:
        row = raw.to_dict()
        _require(
            isinstance(row, Mapping),
            "legacy_queue_only_manifest_source_invalid",
        )
        result = dict(row)
        result["_rebuild_manifest_created_at"] = created_at
        return result

    expected_base = tuple(
        projected(row) for row in tuple(manifest.materializable_sources)
    )
    closure_candidates: dict[str, dict[str, Any]] = {}
    for raw in tuple(manifest.skipped_expired_working):
        if (
            getattr(raw, "artifact_type", None) != "code_evidence"
            or getattr(raw, "source_artifact_status", None) != "superseded"
        ):
            continue
        candidate = projected(raw)
        candidate["_rebuild_dependency_closure"] = _EVIDENCE_CLOSURE_CANDIDATE
        artifact_id = str(candidate.get("id") or "")
        _require(
            bool(artifact_id) and artifact_id not in closure_candidates,
            "legacy_queue_only_manifest_closure_invalid",
        )
        closure_candidates[artifact_id] = candidate

    command_base: list[dict[str, Any]] = []
    command_closure: list[dict[str, Any]] = []
    closure_ids: set[str] = set()
    for raw in command.source_rows:
        row = dict(raw)
        marker = row.get("_rebuild_dependency_closure")
        if marker is None:
            command_base.append(row)
            continue
        artifact_id = str(row.get("id") or "")
        _require(
            marker == _EVIDENCE_CLOSURE_CANDIDATE
            and artifact_id in closure_candidates
            and artifact_id not in closure_ids
            and row == closure_candidates[artifact_id],
            "legacy_queue_only_manifest_closure_invalid",
            artifact_id,
        )
        closure_ids.add(artifact_id)
        command_closure.append(row)
    _require(
        tuple(command_base) == expected_base
        and command_closure
        == [closure_candidates[artifact_id] for artifact_id in sorted(closure_ids)],
        "legacy_queue_only_manifest_command_mismatch",
    )


def _assert_legacy_intent_memberships_bound_to_command(
    intent: Any,
    command: Any,
) -> None:
    """Bind each residual tombstone membership to its checkpoint source row."""

    from okto_pulse.core.kg.board_rebuild_adapter import queue_artifact_type

    command_sources: dict[tuple[str, str], Mapping[str, Any]] = {}
    for source_row in command.source_rows:
        identity = (
            queue_artifact_type(str(source_row.get("artifact_type") or "")),
            str(source_row.get("id") or ""),
        )
        _require(
            all(identity) and identity not in command_sources,
            "legacy_queue_only_command_source_identity_invalid",
        )
        command_sources[identity] = source_row
    memberships = {
        str(membership["row_id"]): membership for membership in intent.memberships
    }
    _require(
        len(memberships) == len(intent.queue_rows),
        "legacy_queue_only_membership_command_mismatch",
    )
    for queue_row in intent.queue_rows:
        row_id = str(queue_row["id"])
        identity = (
            str(queue_row["artifact_type"]),
            str(queue_row["artifact_id"]),
        )
        membership = memberships.get(row_id)
        source_row = command_sources.get(identity)
        _require(
            membership is not None
            and source_row is not None
            and {
                "row_id": row_id,
                "artifact_type": identity[0],
                "artifact_id": identity[1],
                "run_id": command.manifest_ref,
                "source_ref": str(source_row.get("source_ref") or ""),
                "source_version": str(source_row.get("source_version") or ""),
                "content_hash": str(source_row.get("content_hash") or ""),
            }
            == dict(membership),
            "legacy_queue_only_membership_command_mismatch",
            row_id,
        )


def _bounded_legacy_query_rows(
    cursor: sqlite3.Cursor,
    columns: Sequence[str],
    *,
    code: str,
    max_rows: int | None = None,
) -> tuple[tuple[object, ...], ...]:
    """Read a queue/DLQ evidence cut with deterministic resource bounds."""

    rows: list[tuple[object, ...]] = []
    total_bytes = 0
    row_limit = MAX_LEGACY_PROTECTED_QUEUE_ROWS if max_rows is None else max_rows
    for raw in cursor:
        _require(len(rows) < row_limit, f"{code}_row_limit_exceeded")
        values = tuple(raw[index] for index in range(len(columns)))
        try:
            encoded = _canonical_json_bytes(values)
        except RecoveryRefused:
            raise
        except (TypeError, ValueError) as exc:
            raise RecoveryRefused(f"{code}_noncanonical") from exc
        total_bytes += len(encoded)
        _require(
            total_bytes <= MAX_LEGACY_PROTECTED_QUEUE_BYTES,
            f"{code}_byte_limit_exceeded",
        )
        rows.append(values)
    return tuple(rows)


def _legacy_queue_current_evidence(
    db_path: Path,
    *,
    board_id: str,
    source: str,
    source_rows: Sequence[Mapping[str, Any]],
) -> tuple[
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, str], ...],
    str,
    str,
]:
    """Take the WAL-aware exact active legacy row cut used by the intent."""

    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        LEGACY_QUEUE_COLUMNS,
        canonical_evidence_hash,
    )
    from okto_pulse.core.kg.board_rebuild_adapter import queue_artifact_type

    membership_sources: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in source_rows:
        identity = (
            queue_artifact_type(str(row.get("artifact_type") or "")),
            str(row.get("id") or ""),
        )
        _require(
            identity not in membership_sources,
            "legacy_queue_only_source_identity_duplicate",
        )
        membership_sources[identity] = row

    projection = ", ".join(f'"{column}"' for column in LEGACY_QUEUE_COLUMNS)
    with _sqlite_connect_readonly(db_path) as connection:
        columns = tuple(
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(consolidation_queue)"
            ).fetchall()
        )
        _require(
            set(columns) == set(LEGACY_QUEUE_COLUMNS),
            "legacy_queue_only_queue_schema_mismatch",
        )
        fetched = _bounded_legacy_query_rows(
            connection.execute(
                f"SELECT {projection} FROM consolidation_queue "
                "WHERE board_id=? AND source=? ORDER BY id",
                (board_id, source),
            ),
            LEGACY_QUEUE_COLUMNS,
            code="legacy_queue_only_active_rows",
            max_rows=4_096,
        )
        _require(bool(fetched), "legacy_queue_only_active_rows_missing")
        rows = tuple(
            {column: raw[index] for index, column in enumerate(LEGACY_QUEUE_COLUMNS)}
            for raw in fetched
        )
        _require(
            all(row["status"] in {"pending", "claimed"} for row in rows),
            "legacy_queue_only_initial_row_state_invalid",
        )
        active_sources = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source FROM consolidation_queue "
                "WHERE board_id=? AND work_kind='consolidate' "
                "AND status IN ('pending','claimed') "
                "AND source LIKE 'rebuild:%' ORDER BY source LIMIT 2",
                (board_id,),
            ).fetchall()
        )
        _require(
            active_sources == (source,),
            "legacy_queue_only_active_rebuild_source_conflict",
        )
        target_ids = tuple(str(row["id"]) for row in rows)
        placeholders = ",".join("?" for _ in target_ids)
        non_target = _bounded_legacy_query_rows(
            connection.execute(
                f"SELECT {projection} FROM consolidation_queue WHERE board_id=? "
                f"AND id NOT IN ({placeholders}) ORDER BY id",
                (board_id, *target_ids),
            ),
            LEGACY_QUEUE_COLUMNS,
            code="legacy_queue_only_non_target_rows",
        )
        peer_identities = {
            (str(row["artifact_type"]), str(row["artifact_id"])) for row in rows
        }
        for artifact_type, artifact_id in peer_identities:
            peer_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM consolidation_queue WHERE board_id=? "
                    "AND work_kind='consolidate' AND artifact_type=? AND artifact_id=?",
                    (board_id, artifact_type, artifact_id),
                ).fetchone()[0]
            )
            _require(
                peer_count == 1,
                "legacy_queue_only_peer_identity_conflict",
                f"{artifact_type}:{artifact_id}",
            )
        dlq_columns = tuple(
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(consolidation_dead_letter)"
            ).fetchall()
        )
        if dlq_columns:
            dlq_projection = ", ".join(f'"{column}"' for column in dlq_columns)
            dlq_rows = _bounded_legacy_query_rows(
                connection.execute(
                    f"SELECT {dlq_projection} FROM consolidation_dead_letter "
                    "WHERE board_id=? ORDER BY id",
                    (board_id,),
                ),
                dlq_columns,
                code="legacy_queue_only_dlq_rows",
            )
            for artifact_type, artifact_id in peer_identities:
                conflict = connection.execute(
                    "SELECT 1 FROM consolidation_dead_letter WHERE board_id=? "
                    "AND (artifact_type=? AND artifact_id=? OR original_queue_id IN ("
                    + placeholders
                    + ")) LIMIT 1",
                    (board_id, artifact_type, artifact_id, *target_ids),
                ).fetchone()
                _require(
                    conflict is None,
                    "legacy_queue_only_dlq_identity_conflict",
                )
        else:
            dlq_rows = ()

    memberships: list[Mapping[str, str]] = []
    for row in rows:
        identity = (str(row["artifact_type"]), str(row["artifact_id"]))
        source_row = membership_sources.get(identity)
        _require(
            source_row is not None,
            "legacy_queue_only_row_not_in_checkpoint",
            f"{identity[0]}:{identity[1]}",
        )
        assert source_row is not None
        content_hash = str(source_row.get("content_hash") or "")
        _require(
            re.fullmatch(r"[0-9a-f]{64}", content_hash) is not None,
            "legacy_queue_only_checkpoint_content_hash_invalid",
        )
        memberships.append(
            {
                "row_id": str(row["id"]),
                "artifact_type": identity[0],
                "artifact_id": identity[1],
                "run_id": source.removeprefix("rebuild:"),
                "source_ref": str(source_row.get("source_ref") or ""),
                "source_version": str(source_row.get("source_version") or ""),
                "content_hash": content_hash,
            }
        )
    paired = sorted(
        zip(rows, memberships, strict=True), key=lambda pair: str(pair[0]["id"])
    )
    sorted_rows = tuple(dict(row) for row, _membership in paired)
    sorted_memberships = tuple(dict(membership) for _row, membership in paired)
    non_target_fingerprint = canonical_evidence_hash(
        {
            "columns": list(LEGACY_QUEUE_COLUMNS),
            "rows": [list(raw) for raw in non_target],
        }
    )
    dlq_fingerprint = canonical_evidence_hash(
        {
            "columns": list(dlq_columns),
            "rows": [list(raw) for raw in dlq_rows],
        }
    )
    return (
        sorted_rows,
        sorted_memberships,
        non_target_fingerprint,
        dlq_fingerprint,
    )


def _legacy_checkpoint_candidate(
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    board_id: str,
) -> tuple[str, dict[str, Any], Any] | None:
    candidates: list[tuple[str, dict[str, Any], Any]] = []
    for relative in sorted(rebuild_baseline):
        if re.fullmatch(r"audit/f06-checkpoint-[0-9a-f]{24}\.json", relative) is None:
            continue
        checkpoint = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            relative,
            code="legacy_queue_only_checkpoint",
        )
        _require(
            set(checkpoint)
            == {
                "kind",
                "command",
                "state",
                "started_at",
                "last_progress_at",
                "best_queue_depth",
                "last_sequence",
                "queue_progress_events",
                "queue_grace_applied",
                "queue_grace_reason",
                "writer_handoff_count",
                "writer_reacquire_count",
                "compensation_failed_state",
                "compensation_failure_code",
                "compensation_failure_detail",
                "compensation_actions",
                "receipts",
            },
            "legacy_queue_only_checkpoint_shape_invalid",
        )
        command_payload = checkpoint.get("command")
        if (
            not isinstance(command_payload, Mapping)
            or str(command_payload.get("board_id")) != board_id
        ):
            continue
        state = str(checkpoint.get("state") or "")
        if state not in LEGACY_QUEUE_ONLY_CHECKPOINT_STATES:
            continue
        _require(
            checkpoint.get("kind") == "f06_rebuild_checkpoint",
            "legacy_queue_only_checkpoint_kind_invalid",
        )
        try:
            started_at = datetime.fromisoformat(str(checkpoint["started_at"]))
            last_progress_at = datetime.fromisoformat(
                str(checkpoint["last_progress_at"])
            )
        except ValueError as exc:
            raise RecoveryRefused(
                "legacy_queue_only_checkpoint_timestamp_invalid"
            ) from exc
        _require(
            started_at.tzinfo is not None
            and last_progress_at.tzinfo is not None
            and last_progress_at >= started_at
            and type(checkpoint.get("best_queue_depth")) is int
            and checkpoint.get("best_queue_depth") >= 1
            and type(checkpoint.get("last_sequence")) is int
            and checkpoint.get("last_sequence") >= 0
            and type(checkpoint.get("queue_progress_events")) is int
            and checkpoint.get("queue_progress_events") >= 0
            and type(checkpoint.get("queue_grace_applied")) is bool
            and (
                checkpoint.get("queue_grace_reason") is None
                or (
                    isinstance(checkpoint.get("queue_grace_reason"), str)
                    and bool(checkpoint.get("queue_grace_reason"))
                )
            ),
            "legacy_queue_only_checkpoint_progress_invalid",
        )
        _require(
            type(checkpoint.get("writer_handoff_count")) is int
            and checkpoint.get("writer_handoff_count") == 0
            and type(checkpoint.get("writer_reacquire_count")) is int
            and checkpoint.get("writer_reacquire_count") == 0,
            "legacy_queue_only_checkpoint_writer_history_invalid",
        )
        command = _legacy_command_from_checkpoint(checkpoint, board_id=board_id)
        expected_relative = (
            "audit/f06-checkpoint-"
            + hashlib.sha256(command.run_id.encode("utf-8")).hexdigest()[:24]
            + ".json"
        )
        _require(
            relative == expected_relative,
            "legacy_queue_only_checkpoint_path_invalid",
            relative,
        )
        candidates.append((relative, checkpoint, command))
    _require(
        len(candidates) <= 1,
        "legacy_queue_only_checkpoint_cardinality_invalid",
        str(len(candidates)),
    )
    return candidates[0] if candidates else None


def _legacy_validate_prefix_receipts(
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    checkpoint: Mapping[str, Any],
    command: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    receipts = checkpoint.get("receipts")
    _require(isinstance(receipts, Mapping), "legacy_queue_only_receipts_invalid")
    assert isinstance(receipts, Mapping)
    prefix_effects = {
        "snapshot": "snapshot",
        "quarantine": "quarantine",
        "enqueue": "enqueue",
    }
    normalized: dict[str, Any] = {}
    evidence: dict[str, dict[str, str]] = {}
    for label, effect in prefix_effects.items():
        effect_key = f"{command.run_id}:{label}"
        raw_receipt = receipts.get(effect_key)
        _require(
            isinstance(raw_receipt, Mapping),
            "legacy_queue_only_prefix_receipt_missing",
            effect_key,
        )
        receipt = _legacy_receipt_from_payload(
            raw_receipt,
            code="legacy_queue_only_prefix_receipt_invalid",
        )
        _require(
            receipt.effect_key == effect_key
            and receipt.effect == effect
            and receipt.ok is True,
            "legacy_queue_only_prefix_receipt_invalid",
            effect_key,
        )
        details = dict(receipt.details)
        if label == "snapshot":
            _require(
                receipt.code == "ok"
                and details.get("board_id") == command.board_id
                and details.get("readable") is True
                and isinstance(details.get("nodes"), list)
                and isinstance(details.get("edges"), list),
                "legacy_queue_only_snapshot_receipt_invalid",
            )
        elif label == "quarantine":
            affected = details.get("affected_files")
            quarantine_ref = details.get("quarantine_ref")
            _require(
                receipt.code == "ok"
                and isinstance(affected, list)
                and tuple(affected) == ("graph.lbug",)
                and isinstance(quarantine_ref, str)
                and re.fullmatch(r"q_[A-Za-z0-9_-]+", quarantine_ref) is not None,
                "legacy_queue_only_quarantine_receipt_invalid",
            )
        else:
            baseline_ids = details.get("baseline_dead_letter_ids")
            _require(
                receipt.code == "ok"
                and type(details.get("queue_order_version")) is int
                and details.get("queue_order_version") == 4
                and details.get("enqueue_admission_complete") is True
                and isinstance(baseline_ids, list)
                and all(isinstance(value, str) for value in baseline_ids)
                and len(set(baseline_ids)) == len(baseline_ids),
                "legacy_queue_only_enqueue_receipt_invalid",
            )
        relative = _legacy_effect_relative(effect_key)
        artifact = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            relative,
            code="legacy_queue_only_prefix_effect",
        )
        _require(
            artifact == dict(raw_receipt),
            "legacy_queue_only_prefix_effect_split_brain",
            effect_key,
        )
        normalized[effect_key] = receipt
        evidence[label] = {
            "effect_key": effect_key,
            "relative": relative,
            "sha256": rebuild_baseline[relative],
        }

    audit_key = f"{command.run_id}:audit:lease_lost"
    audit_relative = _legacy_effect_relative(audit_key)
    audit_payload = _read_baseline_json(
        rebuild_root,
        rebuild_baseline,
        audit_relative,
        code="legacy_queue_only_lease_lost_audit",
    )
    audit_receipt = _legacy_receipt_from_payload(
        audit_payload,
        code="legacy_queue_only_lease_lost_audit_invalid",
    )
    _require(
        audit_receipt.effect_key == audit_key
        and audit_receipt.effect == "audit"
        and audit_receipt.ok is True
        and audit_receipt.code == "ok"
        and dict(audit_receipt.details).get("state") == "blocked"
        and dict(audit_receipt.details).get("code") == "lease_lost"
        and dict(audit_receipt.details).get("promotion_allowed") is False
        and tuple(dict(audit_receipt.details).get("compensation_actions") or ()) == (),
        "legacy_queue_only_lease_lost_audit_invalid",
    )
    evidence["lease_lost_audit"] = {
        "effect_key": audit_key,
        "relative": audit_relative,
        "sha256": rebuild_baseline[audit_relative],
    }
    return normalized, evidence


def _legacy_validate_run_effect_artifacts(
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    intent: Any,
    checkpoint: Mapping[str, Any],
) -> frozenset[str]:
    """Deny every standalone F06 effect outside the nominal legacy lane."""

    prefix = f"{intent.f06_run_id}:"
    by_key: dict[str, tuple[str, dict[str, Any]]] = {}
    for relative in sorted(rebuild_baseline):
        if re.fullmatch(r"audit/f06-effect-[0-9a-f]{24}\.json", relative) is None:
            continue
        payload = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            relative,
            code="legacy_queue_only_effect_artifact",
        )
        effect_key = payload.get("effect_key")
        _require(
            isinstance(effect_key, str) and bool(effect_key),
            "legacy_queue_only_effect_artifact_invalid",
            relative,
        )
        if not effect_key.startswith(prefix):
            continue
        _require(
            relative == _legacy_effect_relative(effect_key)
            and effect_key not in by_key,
            "legacy_queue_only_effect_artifact_conflict",
            relative,
        )
        by_key[effect_key] = (relative, payload)

    intent_payload = _legacy_intent_receipt_payload(intent)
    compensation_payload = _legacy_terminal_compensation_payload(intent)
    audit_payload = _legacy_nominal_audit_payload(intent)
    failure_audit_key = f"{intent.f06_run_id}:audit:compensation_failed"
    expected_payloads = {
        f"{intent.f06_run_id}:snapshot": None,
        f"{intent.f06_run_id}:quarantine": None,
        f"{intent.f06_run_id}:enqueue": None,
        f"{intent.f06_run_id}:audit:lease_lost": None,
        str(intent_payload["effect_key"]): intent_payload,
        str(compensation_payload["effect_key"]): compensation_payload,
        str(audit_payload["effect_key"]): audit_payload,
        failure_audit_key: None,
    }
    required_prefix = {
        f"{intent.f06_run_id}:snapshot",
        f"{intent.f06_run_id}:quarantine",
        f"{intent.f06_run_id}:enqueue",
        f"{intent.f06_run_id}:audit:lease_lost",
    }
    _require(
        required_prefix <= set(by_key) <= set(expected_payloads),
        "legacy_queue_only_effect_artifact_set_invalid",
        repr(sorted(set(by_key) - set(expected_payloads))),
    )
    for effect_key, expected in expected_payloads.items():
        if expected is None or effect_key not in by_key:
            continue
        _require(
            by_key[effect_key][1] == expected,
            "legacy_queue_only_effect_artifact_payload_invalid",
            effect_key,
        )
    intent_key = str(intent_payload["effect_key"])
    compensation_key = str(compensation_payload["effect_key"])
    audit_key = str(audit_payload["effect_key"])
    _require(
        (compensation_key not in by_key or intent_key in by_key)
        and (audit_key not in by_key or compensation_key in by_key),
        "legacy_queue_only_effect_artifact_order_invalid",
    )
    if failure_audit_key in by_key:
        receipts = checkpoint.get("receipts")
        _require(
            isinstance(receipts, Mapping)
            and intent_key in by_key
            and str(checkpoint.get("state"))
            in {"compensating", "compensation_failed", "failed"},
            "legacy_queue_only_compensation_failed_audit_unexpected",
        )
        _assert_legacy_historical_failure_audit(
            intent,
            by_key[failure_audit_key][1],
        )
    return frozenset(by_key)


def _legacy_terminal_run_evidence(
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    board_id: str,
    command: Any,
    manifest_payload: Mapping[str, Any],
) -> dict[str, Any]:
    matches: list[tuple[str, dict[str, Any]]] = []
    for relative in sorted(rebuild_baseline):
        if re.fullmatch(r"audit/run_[A-Za-z0-9_-]+\.json", relative) is None:
            continue
        audit = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            relative,
            code="legacy_queue_only_terminal_run_audit",
        )
        if (
            str(audit.get("board_id")) == board_id
            and str(audit.get("manifest_ref")) == command.manifest_ref
        ):
            matches.append((relative, audit))
    _require(
        len(matches) == 1,
        "legacy_queue_only_terminal_run_cardinality_invalid",
        str(len(matches)),
    )
    audit_relative, audit = matches[0]
    run_id = str(audit.get("run_id") or "")
    expected_report_id = (
        "report_"
        + hashlib.sha256(f"{board_id}\x1f{run_id}".encode("utf-8")).hexdigest()[:32]
    )
    report_id = str(audit.get("report_id") or "")
    report_relative = _relative_rebuild_reference(
        audit.get("report_ref"),
        rebuild_root=rebuild_root,
        prefix="reports/",
        code="legacy_queue_only_report_reference_invalid",
    )
    _require(
        re.fullmatch(r"run_[A-Za-z0-9_-]+", run_id) is not None
        and report_id == expected_report_id
        and report_relative == f"reports/{report_id}.json"
        and audit_relative == f"audit/{run_id}.json"
        and audit.get("outcome") == "rebuild_failed"
        and audit.get("reason") == "lease_lost"
        and audit.get("event_emitted") is True
        and audit.get("current_kg_generation_id") is None
        and audit.get("previous_kg_generation_id") is None
        and audit.get("promotion_outcome") is None
        and str(audit.get("actor_id")) == command.actor_id
        and str(audit.get("operation")) == OPERATION,
        "legacy_queue_only_terminal_run_invalid",
    )
    confirmation_ref = str(audit.get("confirmation_ref") or "")
    _require(
        re.fullmatch(r"conf_fp_[0-9a-f]{64}", confirmation_ref) is not None,
        "legacy_queue_only_confirmation_ref_invalid",
    )
    report = _read_baseline_json(
        rebuild_root,
        rebuild_baseline,
        report_relative,
        code="legacy_queue_only_report",
    )
    summary = report.get("summary")
    _require(
        report.get("report_id") == report_id
        and isinstance(summary, Mapping)
        and str(summary.get("board_id")) == board_id
        and str(summary.get("run_id")) == run_id
        and str(summary.get("status")) == "failed",
        "legacy_queue_only_report_invalid",
    )
    confirmation_matches: list[tuple[str, dict[str, Any]]] = []
    confirmation_prefix = f"audit/confirmation/{board_id}/"
    for relative in sorted(rebuild_baseline):
        if (
            not relative.startswith(confirmation_prefix)
            or re.fullmatch(
                rf"{re.escape(confirmation_prefix)}audit_[0-9a-f]{{32}}\.json",
                relative,
            )
            is None
        ):
            continue
        payload = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            relative,
            code="legacy_queue_only_confirmation_audit",
        )
        if payload.get("confirmation_ref") == confirmation_ref:
            confirmation_matches.append((relative, payload))
    _require(
        len(confirmation_matches) == 1,
        "legacy_queue_only_confirmation_audit_cardinality_invalid",
        str(len(confirmation_matches)),
    )
    confirmation_relative, confirmation = confirmation_matches[0]
    _require(
        confirmation.get("board_id") == board_id
        and confirmation.get("operation") == OPERATION
        and confirmation.get("outcome") == "consumed"
        and confirmation.get("preflight_hash") == manifest_payload.get("preflight_hash")
        and confirmation.get("confirmation_ref") == confirmation_ref,
        "legacy_queue_only_confirmation_audit_invalid",
    )
    return {
        "run_id": run_id,
        "audit_relative": audit_relative,
        "audit_sha256": rebuild_baseline[audit_relative],
        "report_id": report_id,
        "report_relative": report_relative,
        "report_sha256": rebuild_baseline[report_relative],
        "confirmation_ref": confirmation_ref,
        "confirmation_audit_relative": confirmation_relative,
        "confirmation_audit_sha256": rebuild_baseline[confirmation_relative],
        "outcome": "rebuild_failed",
        "reason": "lease_lost",
        "event_emitted": True,
        "previous_generation_id": None,
        "current_generation_id": None,
        "promotion_outcome": None,
        "current_generation_pointer_present": False,
        "candidate_decision_present": False,
    }


def _legacy_quarantine_evidence(
    *,
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    board_id: str,
    command: Any,
    quarantine_receipt: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    quarantine_details = dict(quarantine_receipt.details)
    original_id = str(quarantine_details.get("quarantine_ref") or "")
    _require(
        re.fullmatch(r"q_[A-Za-z0-9_-]+", original_id) is not None
        and tuple(quarantine_details.get("affected_files") or ()),
        "legacy_queue_only_original_quarantine_receipt_invalid",
    )
    original_manifest_relative = f"{original_id}/manifest.json"
    original_manifest = _read_baseline_json(
        quarantine_root,
        quarantine_baseline,
        original_manifest_relative,
        code="legacy_queue_only_original_quarantine_manifest",
    )
    original_storage = {
        relative: digest
        for relative, digest in quarantine_baseline.items()
        if relative.startswith(f"{original_id}/")
        and relative != original_manifest_relative
    }
    _require(
        set(original_storage) == {f"{original_id}/graph.lbug"}
        and original_manifest.get("quarantine_id") == original_id
        and original_manifest.get("board_id") == board_id
        and original_manifest.get("graph_type") == "board_graph"
        and original_manifest.get("reason")
        == f"explicit_rebuild:{command.manifest_ref}",
        "legacy_queue_only_original_quarantine_invalid",
    )

    manual_matches: list[
        tuple[str, dict[str, Any], dict[str, Any], dict[str, str]]
    ] = []
    for relative in sorted(quarantine_baseline):
        match = re.fullmatch(r"(q_[A-Za-z0-9_-]+)/restore_operation\.json", relative)
        if match is None:
            continue
        manual_id = match.group(1)
        journal = _read_baseline_json(
            quarantine_root,
            quarantine_baseline,
            relative,
            code="legacy_queue_only_manual_restore_journal",
        )
        if (
            journal.get("operation") != "quarantine_restore"
            or journal.get("source_quarantine_id") != original_id
            or journal.get("board_id") != board_id
        ):
            continue
        manifest_relative = f"{manual_id}/manifest.json"
        manifest = _read_baseline_json(
            quarantine_root,
            quarantine_baseline,
            manifest_relative,
            code="legacy_queue_only_manual_restore_manifest",
        )
        storage = {
            candidate: digest
            for candidate, digest in quarantine_baseline.items()
            if candidate.startswith(f"{manual_id}/")
            and candidate not in {relative, manifest_relative}
        }
        manual_matches.append((manual_id, journal, manifest, storage))
    _require(
        len(manual_matches) == 1,
        "legacy_queue_only_manual_restore_cardinality_invalid",
        str(len(manual_matches)),
    )
    manual_id, journal, manual_manifest, manual_storage = manual_matches[0]
    moved = tuple(str(value) for value in journal.get("moved_to_backup") or ())
    copied = tuple(str(value) for value in journal.get("copied_from_snapshot") or ())
    _require(
        journal.get("backup_quarantine_id") == manual_id
        and journal.get("compensation_run_id") is None
        and journal.get("phase") == "done"
        and bool(journal.get("finished_at"))
        and tuple(journal.get("pending_backup") or ()) == ()
        and tuple(journal.get("pending_copy") or ()) == ()
        and journal.get("open_validated") is True
        and journal.get("error") is None
        and journal.get("rollback_instruction") is None
        and set(moved) == {"graph.lbug", "graph.lbug.wal"}
        and copied == ("graph.lbug",)
        and set(manual_storage)
        == {f"{manual_id}/graph.lbug", f"{manual_id}/graph.lbug.wal"}
        and manual_manifest.get("quarantine_id") == manual_id
        and manual_manifest.get("board_id") == board_id
        and manual_manifest.get("graph_type") == "board_graph"
        and manual_manifest.get("reason") == f"restore_backup_swap:{original_id}"
        and manual_manifest.get("reason_bucket") == "operator_manual"
        and tuple(manual_manifest.get("correlation_ids") or ()) == (original_id,)
        and set(manual_manifest.get("affected_paths_relative") or ()) == set(moved)
        and int(manual_manifest.get("files_moved") or -1) == len(moved),
        "legacy_queue_only_manual_restore_invalid",
    )
    return (
        {
            "quarantine_id": original_id,
            "manifest_relative": original_manifest_relative,
            "manifest_sha256": quarantine_baseline[original_manifest_relative],
            "storage_hashes": dict(sorted(original_storage.items())),
        },
        {
            "quarantine_id": manual_id,
            "manifest_relative": f"{manual_id}/manifest.json",
            "manifest_sha256": quarantine_baseline[f"{manual_id}/manifest.json"],
            "journal_relative": f"{manual_id}/restore_operation.json",
            "journal_sha256": quarantine_baseline[
                f"{manual_id}/restore_operation.json"
            ],
            "source_quarantine_id": original_id,
            "storage_hashes": dict(sorted(manual_storage.items())),
        },
    )


def _assert_legacy_intent_evidence_semantics(
    intent: Any,
    command: Any,
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    prefix_receipts: Mapping[str, Any],
) -> None:
    """Reconstruct the physical legacy proof instead of trusting its hashes.

    The intent digest proves only that its own payload is self-consistent.  On
    every replay we must also parse the exact bytes named by that payload and
    prove that they still describe the one failed/non-promoted run and the one
    completed manual restore that authorize the nominal queue-only lane.
    """

    payload = intent.to_payload()
    manifest_evidence = dict(payload["manifest"])
    manifest_relative = str(manifest_evidence["relative"])
    manifest_payload = _read_baseline_json(
        rebuild_root,
        rebuild_baseline,
        manifest_relative,
        code="legacy_queue_only_manifest",
    )
    _require(
        rebuild_baseline.get(manifest_relative) == manifest_evidence["sha256"]
        and manifest_payload.get("board_id") == intent.board_id
        and manifest_payload.get("preflight_hash")
        == manifest_evidence["preflight_hash"]
        and manifest_payload.get("source_set_hash")
        == manifest_evidence["source_set_hash"],
        "legacy_queue_only_manifest_evidence_changed",
    )
    terminal_run = _legacy_terminal_run_evidence(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild_baseline,
        board_id=intent.board_id,
        command=command,
        manifest_payload=manifest_payload,
    )
    quarantine_key = f"{command.run_id}:quarantine"
    quarantine_receipt = prefix_receipts.get(quarantine_key)
    _require(
        quarantine_receipt is not None,
        "legacy_queue_only_quarantine_receipt_missing",
    )
    original_quarantine, manual_restore = _legacy_quarantine_evidence(
        quarantine_root=quarantine_root,
        quarantine_baseline=quarantine_baseline,
        board_id=intent.board_id,
        command=command,
        quarantine_receipt=quarantine_receipt,
    )
    _require(
        terminal_run == dict(payload["terminal_run"])
        and original_quarantine == dict(payload["original_quarantine"])
        and manual_restore == dict(payload["manual_restore"]),
        "legacy_queue_only_semantic_evidence_mismatch",
    )


def _legacy_intent_receipt_payload(intent: Any) -> dict[str, Any]:
    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        LEGACY_QUEUE_ONLY_INTENT_CODE,
        LEGACY_QUEUE_ONLY_INTENT_EFFECT,
    )

    return {
        "effect_key": f"{intent.f06_run_id}:{LEGACY_QUEUE_ONLY_INTENT_EFFECT}",
        "effect": LEGACY_QUEUE_ONLY_INTENT_EFFECT,
        "ok": True,
        "code": LEGACY_QUEUE_ONLY_INTENT_CODE,
        "details": intent.to_payload(),
    }


def _legacy_terminal_compensation_payload(intent: Any) -> dict[str, Any]:
    pending = sum(1 for row in intent.queue_rows if row["status"] == "pending")
    claimed = sum(1 for row in intent.queue_rows if row["status"] == "claimed")
    return {
        "effect_key": f"{intent.f06_run_id}:compensate",
        "effect": "compensate",
        "ok": True,
        "code": LEGACY_QUEUE_ONLY_OUTCOME_CODE,
        "details": {
            "actions": ["cancel_enqueued_sources"],
            "reconciliation_kind": "legacy_manual_restore_queue_only",
            "intent_digest": intent.evidence_digest,
            "queue": {
                "source": intent.queue_source,
                "expected_row_count": len(intent.queue_rows),
                "terminal_fingerprint": intent.terminal_queue_fingerprint,
                "pending_compensated": pending,
                "claimed_compensated": claimed,
                "already_compensated": 0,
                "active_remaining": 0,
                "live_intents_restored": 0,
                "total_compensated": pending + claimed,
                "evidence_digest": intent.evidence_digest,
            },
        },
    }


def _legacy_nominal_audit_payload(intent: Any) -> dict[str, Any]:
    return {
        "effect_key": (f"{intent.f06_run_id}:audit:{LEGACY_QUEUE_ONLY_OUTCOME_CODE}"),
        "effect": "audit",
        "ok": True,
        "code": "ok",
        "details": {
            "state": "failed",
            "code": LEGACY_QUEUE_ONLY_OUTCOME_CODE,
            "promotion_allowed": False,
            "compensation_actions": ["cancel_enqueued_sources"],
            "detail": "legacy_blocked_after_enqueue_predecessor_already_restored",
        },
    }


def _assert_legacy_historical_failure_audit(
    intent: Any,
    payload: Mapping[str, Any],
) -> None:
    details = payload.get("details")
    prefix = "legacy_manual_restore_queue_only_compensation_failed:"
    _require(
        set(payload) == {"effect_key", "effect", "ok", "code", "details"}
        and payload.get("effect_key")
        == f"{intent.f06_run_id}:audit:compensation_failed"
        and payload.get("effect") == "audit"
        and payload.get("ok") is True
        and payload.get("code") == "ok"
        and isinstance(details, Mapping)
        and set(details)
        == {
            "state",
            "code",
            "promotion_allowed",
            "compensation_actions",
            "detail",
        }
        and details.get("state") == "compensation_failed"
        and details.get("code") == "compensation_failed"
        and details.get("promotion_allowed") is False
        and tuple(details.get("compensation_actions") or ())
        == ("cancel_enqueued_sources",)
        and isinstance(details.get("detail"), str)
        and str(details["detail"]).startswith(prefix)
        and bool(str(details["detail"])[len(prefix) :]),
        "legacy_queue_only_compensation_failed_audit_invalid",
    )


def _assert_legacy_queue_only_terminal_checkpoint(
    intent: Any,
    checkpoint: Mapping[str, Any],
) -> None:
    """Validate the durable terminal marker before a fresh lane adopts it."""

    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        canonical_evidence_hash,
    )

    intent_payload = _legacy_intent_receipt_payload(intent)
    compensation_payload = _legacy_terminal_compensation_payload(intent)
    terminal_command = _legacy_command_from_checkpoint(
        checkpoint,
        board_id=intent.board_id,
    )
    checkpoint_evidence = dict(intent.payload["checkpoint"])
    receipts = checkpoint.get("receipts")
    _require(
        terminal_command.run_id == intent.f06_run_id
        and terminal_command.manifest_ref == intent.manifest_ref
        and terminal_command.actor_id == intent.payload["legacy_actor_id"]
        and terminal_command.reason == intent.payload["legacy_reason"]
        and terminal_command.candidate_generation_id
        == intent.payload["candidate_generation_id"]
        and canonical_evidence_hash(list(terminal_command.source_rows))
        == checkpoint_evidence["source_rows_sha256"]
        and isinstance(receipts, Mapping)
        and str(checkpoint.get("state")) == "failed"
        and checkpoint.get("compensation_failed_state") == "enqueued"
        and checkpoint.get("compensation_failure_code")
        == LEGACY_QUEUE_ONLY_OUTCOME_CODE
        and checkpoint.get("compensation_failure_detail")
        == "legacy_blocked_after_enqueue_predecessor_already_restored"
        and tuple(checkpoint.get("compensation_actions") or ())
        == ("cancel_enqueued_sources",)
        and set(receipts)
        == {
            f"{intent.f06_run_id}:snapshot",
            f"{intent.f06_run_id}:quarantine",
            f"{intent.f06_run_id}:enqueue",
            str(intent_payload["effect_key"]),
            str(compensation_payload["effect_key"]),
        }
        and dict(receipts[str(intent_payload["effect_key"])]) == intent_payload
        and dict(receipts[str(compensation_payload["effect_key"])])
        == compensation_payload,
        "legacy_queue_only_terminal_checkpoint_invalid",
    )


def _assert_legacy_queue_only_phase_consistency(
    intent: Any,
    checkpoint: Mapping[str, Any],
    *,
    effect_keys: frozenset[str],
    queue_terminal: bool,
) -> None:
    """Prove the only durable orderings the nominal lane can produce."""

    compensation_payload = _legacy_terminal_compensation_payload(intent)
    compensation_key = str(compensation_payload["effect_key"])
    intent_payload = _legacy_intent_receipt_payload(intent)
    intent_key = str(intent_payload["effect_key"])
    audit_key = str(_legacy_nominal_audit_payload(intent)["effect_key"])
    prefix_receipt_keys = {
        f"{intent.f06_run_id}:snapshot",
        f"{intent.f06_run_id}:quarantine",
        f"{intent.f06_run_id}:enqueue",
    }
    prefix_effect_keys = {
        *prefix_receipt_keys,
        f"{intent.f06_run_id}:audit:lease_lost",
    }
    receipts = checkpoint.get("receipts")
    _require(isinstance(receipts, Mapping), "legacy_queue_only_receipts_invalid")
    assert isinstance(receipts, Mapping)
    state = str(checkpoint.get("state") or "")
    if state == "failed":
        _assert_legacy_queue_only_terminal_checkpoint(intent, checkpoint)
    if state == "blocked":
        _require(
            set(receipts) == prefix_receipt_keys
            and not queue_terminal
            and effect_keys
            in {
                frozenset(prefix_effect_keys),
                frozenset({*prefix_effect_keys, intent_key}),
            }
            and checkpoint.get("compensation_failed_state") is None
            and checkpoint.get("compensation_failure_code") is None
            and checkpoint.get("compensation_failure_detail") is None
            and tuple(checkpoint.get("compensation_actions") or ()) == (),
            "legacy_queue_only_blocked_phase_invalid",
        )
    else:
        _require(
            state in {"compensating", "compensation_failed", "failed"}
            and checkpoint.get("compensation_failed_state") == "enqueued"
            and checkpoint.get("compensation_failure_code")
            == LEGACY_QUEUE_ONLY_OUTCOME_CODE
            and checkpoint.get("compensation_failure_detail")
            == "legacy_blocked_after_enqueue_predecessor_already_restored"
            and tuple(checkpoint.get("compensation_actions") or ())
            == ("cancel_enqueued_sources",)
            and isinstance(receipts.get(intent_key), Mapping)
            and dict(receipts[intent_key]) == intent_payload
            and intent_key in effect_keys,
            "legacy_queue_only_durable_context_invalid",
        )
    raw_compensation = receipts.get(compensation_key)
    if state == "compensating":
        _require(
            set(receipts) == {*prefix_receipt_keys, intent_key}
            and raw_compensation is None
            and audit_key not in effect_keys,
            "legacy_queue_only_compensating_phase_invalid",
        )
    elif state == "compensation_failed":
        _require(
            set(receipts) == {*prefix_receipt_keys, intent_key, compensation_key}
            and isinstance(raw_compensation, Mapping)
            and raw_compensation.get("effect_key") == compensation_key
            and raw_compensation.get("effect") == "compensate"
            and raw_compensation.get("ok") is False
            and isinstance(raw_compensation.get("code"), str)
            and bool(raw_compensation.get("code"))
            and raw_compensation.get("code") != LEGACY_QUEUE_ONLY_OUTCOME_CODE
            and audit_key not in effect_keys,
            "legacy_queue_only_compensation_failed_phase_invalid",
        )
    if isinstance(raw_compensation, Mapping) and raw_compensation.get("ok") is True:
        _require(
            dict(raw_compensation) == compensation_payload
            and compensation_key in effect_keys,
            "legacy_queue_only_terminal_compensation_artifact_missing",
        )
    if compensation_key in effect_keys:
        _require(
            queue_terminal,
            "legacy_queue_only_compensation_artifact_without_terminal_queue",
        )
    if audit_key in effect_keys:
        _require(
            queue_terminal,
            "legacy_queue_only_nominal_audit_without_terminal_queue",
        )
        _assert_legacy_queue_only_terminal_checkpoint(intent, checkpoint)


def _build_legacy_queue_only_intent(
    *,
    bundle: ServiceBundle,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    board_storage_baseline: Mapping[str, str],
    db_path: Path,
    data_home: Path,
    board_id: str,
    recovery_actor_id: str,
    recovery_reason: str,
    checkpoint_relative: str,
    checkpoint: Mapping[str, Any],
    command: Any,
) -> tuple[Any, Any]:
    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        LEGACY_QUEUE_ONLY_KIND,
        LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS,
        LEGACY_QUEUE_ONLY_REMAINING_ACTIONS,
        LEGACY_QUEUE_ONLY_SCHEMA,
        LegacyManualRestoreQueueOnlyIntent,
        canonical_evidence_hash,
    )

    prefix_receipts, f06_artifacts = _legacy_validate_prefix_receipts(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild_baseline,
        checkpoint=checkpoint,
        command=command,
    )
    receipts = checkpoint.get("receipts")
    assert isinstance(receipts, Mapping)
    _require(
        set(receipts)
        == {
            f"{command.run_id}:snapshot",
            f"{command.run_id}:quarantine",
            f"{command.run_id}:enqueue",
        }
        and str(checkpoint.get("state")) == "blocked"
        and checkpoint.get("compensation_failed_state") is None
        and checkpoint.get("compensation_failure_code") is None
        and checkpoint.get("compensation_failure_detail") is None
        and tuple(checkpoint.get("compensation_actions") or ()) == (),
        "legacy_queue_only_first_admission_checkpoint_invalid",
    )
    source_rows = tuple(command.source_rows)
    source_rows_sha256 = canonical_evidence_hash(list(source_rows))
    manifest_relative = f"manifests/{command.manifest_ref}.json"
    manifest_payload = _read_baseline_json(
        rebuild_root,
        rebuild_baseline,
        manifest_relative,
        code="legacy_queue_only_manifest",
    )
    preflight_hash = str(manifest_payload.get("preflight_hash") or "")
    source_set_hash = str(manifest_payload.get("source_set_hash") or "")
    _require(
        manifest_payload.get("board_id") == board_id
        and re.fullmatch(r"[0-9a-f]{64}", preflight_hash) is not None
        and re.fullmatch(r"[0-9a-f]{64}", source_set_hash) is not None,
        "legacy_queue_only_manifest_binding_invalid",
    )
    try:
        verified_manifest = bundle.manifest_store.load_verified(
            command.manifest_ref,
            expected_board_id=board_id,
            expected_preflight_hash=preflight_hash,
        )
    except Exception as exc:
        raise RecoveryRefused("legacy_queue_only_manifest_integrity_invalid") from exc
    _require(
        getattr(verified_manifest, "source_set_hash", None) == source_set_hash,
        "legacy_queue_only_manifest_binding_invalid",
    )
    _assert_legacy_command_bound_to_manifest(command, verified_manifest)

    terminal_run = _legacy_terminal_run_evidence(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild_baseline,
        board_id=board_id,
        command=command,
        manifest_payload=manifest_payload,
    )
    original_quarantine, manual_restore = _legacy_quarantine_evidence(
        quarantine_root=quarantine_root,
        quarantine_baseline=quarantine_baseline,
        board_id=board_id,
        command=command,
        quarantine_receipt=prefix_receipts[f"{command.run_id}:quarantine"],
    )
    current_pointer_relative = f"generations/{board_id}/current.json"
    candidate_decision_root = data_home / "candidate_decisions" / board_id
    candidate_decisions = _snapshot_tree_hashes(candidate_decision_root)
    _require(
        current_pointer_relative not in rebuild_baseline and not candidate_decisions,
        "legacy_queue_only_generation_evidence_conflict",
    )
    _require(
        "graph.lbug" in board_storage_baseline
        and set(board_storage_baseline) <= {"graph.lbug", "graph.lbug.wal"},
        "legacy_queue_only_board_storage_invalid",
    )
    source = f"rebuild:{command.manifest_ref}"
    rows, memberships, non_target_fingerprint, dlq_fingerprint = (
        _legacy_queue_current_evidence(
            db_path,
            board_id=board_id,
            source=source,
            source_rows=source_rows,
        )
    )
    evidence = {
        "schema_version": LEGACY_QUEUE_ONLY_SCHEMA,
        "reconciliation_kind": LEGACY_QUEUE_ONLY_KIND,
        "board_id": board_id,
        "recovery_actor_id": recovery_actor_id,
        "recovery_reason": recovery_reason,
        "legacy_actor_id": command.actor_id,
        "legacy_reason": command.reason,
        "manifest_ref": command.manifest_ref,
        "f06_run_id": command.run_id,
        "candidate_generation_id": command.candidate_generation_id,
        "checkpoint": {
            "relative": checkpoint_relative,
            "sha256": rebuild_baseline[checkpoint_relative],
            "source_rows_sha256": source_rows_sha256,
            "state": "blocked",
        },
        "f06_artifacts": f06_artifacts,
        "manifest": {
            "relative": manifest_relative,
            "sha256": rebuild_baseline[manifest_relative],
            "preflight_hash": preflight_hash,
            "source_set_hash": source_set_hash,
        },
        "terminal_run": terminal_run,
        "original_quarantine": original_quarantine,
        "manual_restore": manual_restore,
        "board_storage": {
            "hashes": dict(board_storage_baseline),
            "sha256": canonical_evidence_hash(dict(board_storage_baseline)),
        },
        "queue": {
            "source": source,
            "snapshot_fingerprint": canonical_evidence_hash(
                {"rows": list(rows), "memberships": list(memberships)}
            ),
            "rows": list(rows),
            "memberships": list(memberships),
            "board_non_target_fingerprint": non_target_fingerprint,
            "dlq_fingerprint": dlq_fingerprint,
        },
        "preapplied_actions": list(LEGACY_QUEUE_ONLY_PREAPPLIED_ACTIONS),
        "remaining_actions": list(LEGACY_QUEUE_ONLY_REMAINING_ACTIONS),
    }
    intent = LegacyManualRestoreQueueOnlyIntent.build(evidence)
    _assert_legacy_intent_memberships_bound_to_command(intent, command)
    intent_payload = _legacy_intent_receipt_payload(intent)
    intent_receipt = _legacy_receipt_from_payload(
        intent_payload,
        code="legacy_queue_only_intent_receipt_invalid",
    )
    return intent, intent_receipt


def _legacy_queue_state_current(
    intent: Any,
    *,
    db_path: Path,
) -> tuple[bool, CompensatedQueueAdoption]:
    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        LEGACY_QUEUE_COLUMNS,
        canonical_evidence_hash,
        legacy_queue_terminal_row,
    )

    expected_rows = {str(row["id"]): dict(row) for row in intent.queue_rows}
    memberships = {str(row["row_id"]): dict(row) for row in intent.memberships}
    target_ids = tuple(sorted(expected_rows))
    projection = ", ".join(f'"{column}"' for column in LEGACY_QUEUE_COLUMNS)
    placeholders = ",".join("?" for _ in target_ids)
    with _sqlite_connect_readonly(db_path) as connection:
        current_values = _bounded_legacy_query_rows(
            connection.execute(
                f"SELECT {projection} FROM consolidation_queue "
                "WHERE board_id=? AND source=? ORDER BY id",
                (intent.board_id, intent.queue_source),
            ),
            LEGACY_QUEUE_COLUMNS,
            code="legacy_queue_only_current_target_rows",
            max_rows=len(expected_rows),
        )
        current_rows = {
            str(raw[0]): {
                column: raw[index] for index, column in enumerate(LEGACY_QUEUE_COLUMNS)
            }
            for raw in current_values
        }
        _require(
            set(current_rows) == set(expected_rows),
            "legacy_queue_only_row_set_changed",
        )
        terminal_count = 0
        for row_id, expected in expected_rows.items():
            terminal = legacy_queue_terminal_row(expected, memberships[row_id])
            current = current_rows[row_id]
            _require(
                current in (expected, terminal),
                "legacy_queue_only_target_row_changed",
                row_id,
            )
            if current == terminal:
                terminal_count += 1
            elif expected["status"] == "claimed":
                _require(
                    _parse_queue_timestamp(expected["claim_timeout_at"])
                    < datetime.now(timezone.utc),
                    "legacy_queue_only_claim_not_expired",
                    row_id,
                )
        non_target = _bounded_legacy_query_rows(
            connection.execute(
                f"SELECT {projection} FROM consolidation_queue WHERE board_id=? "
                f"AND id NOT IN ({placeholders}) ORDER BY id",
                (intent.board_id, *target_ids),
            ),
            LEGACY_QUEUE_COLUMNS,
            code="legacy_queue_only_current_non_target_rows",
        )
        non_target_fingerprint = canonical_evidence_hash(
            {
                "columns": list(LEGACY_QUEUE_COLUMNS),
                "rows": [list(raw) for raw in non_target],
            }
        )
        dlq_columns = tuple(
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(consolidation_dead_letter)"
            ).fetchall()
        )
        if dlq_columns:
            dlq_projection = ", ".join(f'"{column}"' for column in dlq_columns)
            dlq_rows = _bounded_legacy_query_rows(
                connection.execute(
                    f"SELECT {dlq_projection} FROM consolidation_dead_letter "
                    "WHERE board_id=? ORDER BY id",
                    (intent.board_id,),
                ),
                dlq_columns,
                code="legacy_queue_only_current_dlq_rows",
            )
        else:
            dlq_rows = ()
        dlq_fingerprint = canonical_evidence_hash(
            {
                "columns": list(dlq_columns),
                "rows": [list(raw) for raw in dlq_rows],
            }
        )
    queue_evidence = dict(intent.payload["queue"])
    _require(
        non_target_fingerprint == queue_evidence["board_non_target_fingerprint"]
        and dlq_fingerprint == queue_evidence["dlq_fingerprint"],
        "legacy_queue_only_protected_queue_changed",
    )
    identities = frozenset(
        (str(row["artifact_type"]), str(row["artifact_id"]))
        for row in intent.queue_rows
    )
    membership_by_identity = {
        (str(row["artifact_type"]), str(row["artifact_id"])): {
            key: str(row[key])
            for key in ("run_id", "source_ref", "source_version", "content_hash")
        }
        for row in intent.memberships
    }
    adoption = CompensatedQueueAdoption(
        source=intent.queue_source,
        manifest_ref=intent.manifest_ref,
        identities=identities,
        membership_by_identity=membership_by_identity,
    )
    _require(
        terminal_count in {0, len(expected_rows)},
        "legacy_queue_only_partial_terminal_queue_invalid",
    )
    return terminal_count == len(expected_rows), adoption


def _assert_legacy_queue_only_evidence_current(
    intent: Any,
    *,
    data_home: Path,
    db_path: Path,
) -> bool:
    """Re-prove immutable/manual evidence and the exact queue cut under fences."""

    rebuild_root = data_home / "rebuild"
    quarantine_root = data_home / "quarantine"
    rebuild = _snapshot_tree_hashes(rebuild_root)
    quarantine = _snapshot_tree_hashes(quarantine_root)
    board = _snapshot_tree_hashes(data_home / "boards" / intent.board_id)
    payload = intent.to_payload()

    immutable_rebuild: dict[str, str] = {}
    manifest = dict(payload["manifest"])
    immutable_rebuild[str(manifest["relative"])] = str(manifest["sha256"])
    for artifact in dict(payload["f06_artifacts"]).values():
        artifact = dict(artifact)
        immutable_rebuild[str(artifact["relative"])] = str(artifact["sha256"])
    terminal = dict(payload["terminal_run"])
    for relative_key, digest_key in (
        ("audit_relative", "audit_sha256"),
        ("report_relative", "report_sha256"),
        ("confirmation_audit_relative", "confirmation_audit_sha256"),
    ):
        immutable_rebuild[str(terminal[relative_key])] = str(terminal[digest_key])
    _require(
        all(
            rebuild.get(relative) == digest
            for relative, digest in immutable_rebuild.items()
        ),
        "legacy_queue_only_rebuild_evidence_changed",
    )

    expected_quarantine: dict[str, str] = {}
    for section in ("original_quarantine", "manual_restore"):
        evidence = dict(payload[section])
        expected_quarantine[str(evidence["manifest_relative"])] = str(
            evidence["manifest_sha256"]
        )
        if section == "manual_restore":
            expected_quarantine[str(evidence["journal_relative"])] = str(
                evidence["journal_sha256"]
            )
        expected_quarantine.update(
            {
                str(key): str(value)
                for key, value in dict(evidence["storage_hashes"]).items()
            }
        )
        quarantine_id = str(evidence["quarantine_id"])
        actual_partition = {
            relative: digest
            for relative, digest in quarantine.items()
            if relative.startswith(f"{quarantine_id}/")
        }
        expected_partition = {
            relative: digest
            for relative, digest in expected_quarantine.items()
            if relative.startswith(f"{quarantine_id}/")
        }
        _require(
            actual_partition == expected_partition,
            "legacy_queue_only_quarantine_evidence_changed",
            quarantine_id,
        )
    expected_board = dict(payload["board_storage"])["hashes"]
    _require(
        board == dict(expected_board),
        "legacy_queue_only_board_storage_changed",
    )
    _require(
        f"generations/{intent.board_id}/current.json" not in rebuild
        and not _snapshot_tree_hashes(
            data_home / "candidate_decisions" / intent.board_id
        ),
        "legacy_queue_only_generation_evidence_changed",
    )
    candidate = _legacy_checkpoint_candidate(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild,
        board_id=intent.board_id,
    )
    _require(
        candidate is not None,
        "legacy_queue_only_checkpoint_missing_during_probe",
    )
    assert candidate is not None
    checkpoint_relative, checkpoint, command = candidate
    checkpoint_evidence = dict(payload["checkpoint"])
    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        canonical_evidence_hash,
    )

    _require(
        checkpoint_relative == checkpoint_evidence["relative"]
        and command.run_id == intent.f06_run_id
        and command.manifest_ref == intent.manifest_ref
        and command.actor_id == payload["legacy_actor_id"]
        and command.reason == payload["legacy_reason"]
        and command.candidate_generation_id == payload["candidate_generation_id"]
        and canonical_evidence_hash(list(command.source_rows))
        == checkpoint_evidence["source_rows_sha256"],
        "legacy_queue_only_checkpoint_changed_during_probe",
    )
    _assert_legacy_intent_memberships_bound_to_command(intent, command)
    prefix_receipts, _prefix_evidence = _legacy_validate_prefix_receipts(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild,
        checkpoint=checkpoint,
        command=command,
    )
    _assert_legacy_intent_evidence_semantics(
        intent,
        command,
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild,
        quarantine_root=quarantine_root,
        quarantine_baseline=quarantine,
        prefix_receipts=prefix_receipts,
    )
    effect_keys = _legacy_validate_run_effect_artifacts(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild,
        intent=intent,
        checkpoint=checkpoint,
    )
    queue_terminal, _adoption = _legacy_queue_state_current(intent, db_path=db_path)
    _assert_legacy_queue_only_phase_consistency(
        intent,
        checkpoint,
        effect_keys=effect_keys,
        queue_terminal=queue_terminal,
    )
    return True


def _discover_legacy_queue_only_reconciliation(
    bundle: ServiceBundle,
    *,
    data_home: Path,
    db_path: Path,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    board_storage_baseline: Mapping[str, str],
    board_id: str,
    recovery_actor_id: str,
    recovery_reason: str,
) -> LegacyQueueOnlyReconciliation | None:
    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        LEGACY_QUEUE_ONLY_INTENT_EFFECT,
        LegacyManualRestoreQueueOnlyIntent,
        canonical_evidence_hash,
    )
    from okto_pulse.core.kg.rebuild_service import (
        RebuildConfirmationReceiptIntegrityError,
        load_verified_rebuild_confirmation_receipt,
    )

    try:
        online_receipt = load_verified_rebuild_confirmation_receipt(
            artifact_store=bundle.artifact_store,
            board_id=board_id,
        )
    except RebuildConfirmationReceiptIntegrityError as exc:
        raise RecoveryRefused(
            "legacy_queue_only_confirmation_namespace_invalid"
        ) from exc
    if online_receipt is not None:
        return None

    candidate = _legacy_checkpoint_candidate(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild_baseline,
        board_id=board_id,
    )
    if candidate is None:
        return None
    checkpoint_relative, checkpoint, command = candidate
    prefix_receipts, _f06_evidence = _legacy_validate_prefix_receipts(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild_baseline,
        checkpoint=checkpoint,
        command=command,
    )
    intent_key = f"{command.run_id}:{LEGACY_QUEUE_ONLY_INTENT_EFFECT}"
    intent_relative = _legacy_effect_relative(intent_key)
    checkpoint_receipts = checkpoint.get("receipts")
    assert isinstance(checkpoint_receipts, Mapping)
    raw_checkpoint_intent = checkpoint_receipts.get(intent_key)
    if intent_relative not in rebuild_baseline:
        _require(
            raw_checkpoint_intent is None,
            "legacy_queue_only_intent_artifact_missing",
        )
        intent, intent_receipt = _build_legacy_queue_only_intent(
            bundle=bundle,
            rebuild_root=rebuild_root,
            rebuild_baseline=rebuild_baseline,
            quarantine_root=quarantine_root,
            quarantine_baseline=quarantine_baseline,
            board_storage_baseline=board_storage_baseline,
            db_path=db_path,
            data_home=data_home,
            board_id=board_id,
            recovery_actor_id=recovery_actor_id,
            recovery_reason=recovery_reason,
            checkpoint_relative=checkpoint_relative,
            checkpoint=checkpoint,
            command=command,
        )
    else:
        raw_intent = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            intent_relative,
            code="legacy_queue_only_intent_artifact",
        )
        intent_receipt = _legacy_receipt_from_payload(
            raw_intent,
            code="legacy_queue_only_intent_receipt_invalid",
        )
        intent = LegacyManualRestoreQueueOnlyIntent.from_payload(intent_receipt.details)
        _require(
            intent_receipt.effect_key == intent_key
            and intent.board_id == board_id
            and intent.manifest_ref == command.manifest_ref
            and intent.f06_run_id == command.run_id
            and intent.payload["legacy_actor_id"] == command.actor_id
            and intent.payload["legacy_reason"] == command.reason
            and intent.payload["candidate_generation_id"]
            == command.candidate_generation_id
            and intent.payload["recovery_actor_id"] == recovery_actor_id
            and raw_checkpoint_intent in (None, raw_intent)
            and dict(intent.payload["checkpoint"])["relative"] == checkpoint_relative
            and dict(intent.payload["checkpoint"])["source_rows_sha256"]
            == canonical_evidence_hash(list(command.source_rows)),
            "legacy_queue_only_existing_intent_binding_invalid",
        )
        manifest_evidence = dict(intent.payload["manifest"])
        try:
            verified_manifest = bundle.manifest_store.load_verified(
                command.manifest_ref,
                expected_board_id=board_id,
                expected_preflight_hash=str(manifest_evidence["preflight_hash"]),
            )
        except Exception as exc:
            raise RecoveryRefused(
                "legacy_queue_only_manifest_integrity_invalid"
            ) from exc
        _require(
            getattr(verified_manifest, "source_set_hash", None)
            == manifest_evidence["source_set_hash"],
            "legacy_queue_only_manifest_binding_invalid",
        )
        _assert_legacy_command_bound_to_manifest(command, verified_manifest)
        _assert_legacy_intent_memberships_bound_to_command(intent, command)
    _assert_legacy_intent_evidence_semantics(
        intent,
        command,
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild_baseline,
        quarantine_root=quarantine_root,
        quarantine_baseline=quarantine_baseline,
        prefix_receipts=prefix_receipts,
    )
    effect_keys = _legacy_validate_run_effect_artifacts(
        rebuild_root=rebuild_root,
        rebuild_baseline=rebuild_baseline,
        intent=intent,
        checkpoint=checkpoint,
    )
    _require(
        _assert_legacy_queue_only_evidence_current(
            intent,
            data_home=data_home,
            db_path=db_path,
        ),
        "legacy_queue_only_evidence_not_current",
    )
    queue_terminal, adoption = _legacy_queue_state_current(intent, db_path=db_path)
    _assert_legacy_queue_only_phase_consistency(
        intent,
        checkpoint,
        effect_keys=effect_keys,
        queue_terminal=queue_terminal,
    )
    compensation_key = f"{command.run_id}:compensate"
    compensation_relative = _legacy_effect_relative(compensation_key)
    raw_compensation = checkpoint_receipts.get(compensation_key)
    nominal_audit_payload = _legacy_nominal_audit_payload(intent)
    nominal_audit_relative = _legacy_effect_relative(
        str(nominal_audit_payload["effect_key"])
    )
    terminal = bool(
        str(checkpoint.get("state")) == "failed"
        and isinstance(raw_compensation, Mapping)
        and compensation_relative in rebuild_baseline
        and queue_terminal
        and nominal_audit_relative in rebuild_baseline
    )
    if terminal:
        _assert_legacy_queue_only_terminal_checkpoint(intent, checkpoint)
        compensation_artifact = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            compensation_relative,
            code="legacy_queue_only_terminal_compensation",
        )
        nominal_audit_artifact = _read_baseline_json(
            rebuild_root,
            rebuild_baseline,
            nominal_audit_relative,
            code="legacy_queue_only_nominal_audit",
        )
        _require(
            compensation_artifact == dict(raw_compensation)
            and compensation_artifact == _legacy_terminal_compensation_payload(intent)
            and nominal_audit_artifact == nominal_audit_payload,
            "legacy_queue_only_terminal_compensation_invalid",
        )
    return LegacyQueueOnlyReconciliation(
        intent=intent,
        command=command,
        intent_receipt=intent_receipt,
        checkpoint_relative=checkpoint_relative,
        checkpoint_baseline=checkpoint,
        terminal=terminal,
        adoption=adoption if terminal else None,
    )


def _read_run_audit(
    bundle: ServiceBundle,
    *,
    board_id: str,
    run_id: str,
) -> Mapping[str, Any] | None:
    from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey

    payload = bundle.artifact_store.read_json(
        RebuildAuditKey(
            namespace="run_audit",
            board_id=board_id,
            artifact_id=run_id,
        )
    )
    return payload if isinstance(payload, Mapping) else None


def _discover_incomplete_receipt(
    bundle: ServiceBundle,
    *,
    board_id: str,
    actor_id: str,
) -> Mapping[str, Any] | None:
    """Select the Core-verified active operation without duplicating schema."""

    from okto_pulse.core.kg.rebuild_service import (
        RebuildConfirmationReceiptIntegrityError,
        is_rebuild_terminal_audit_closed,
        is_rebuild_terminal_audit_frozen,
        is_rebuild_terminal_audit_resumable,
        load_verified_rebuild_confirmation_receipt,
    )

    try:
        receipt = load_verified_rebuild_confirmation_receipt(
            artifact_store=bundle.artifact_store,
            board_id=board_id,
        )
    except RebuildConfirmationReceiptIntegrityError as exc:
        raise RecoveryRefused(
            "rebuild_confirmation_active_receipt_integrity_invalid"
        ) from exc
    if receipt is None:
        # The shared loader proves both the active sentinel and the entire
        # new receipt-history namespace absent.  Legacy rebuild artifacts live
        # outside that namespace and intentionally remain valid bootstrap
        # state for the first recovery-only operation.
        return None

    _require(
        str(receipt.get("actor_id")) == actor_id,
        "rebuild_confirmation_receipt_actor_mismatch",
    )
    _require(
        str(receipt.get("operation")) == OPERATION,
        "rebuild_confirmation_receipt_operation_mismatch",
    )
    run_id = str(receipt["run_id"])
    audit = _read_run_audit(bundle, board_id=board_id, run_id=run_id)
    receipt_state = receipt.get("receipt_state")
    if audit is None:
        _require(
            receipt_state in (None, "authorized"),
            "rebuild_confirmation_terminal_audit_missing",
            run_id,
        )
        return receipt

    _require(
        all(
            (
                str(audit.get("run_id")) == run_id,
                str(audit.get("board_id")) == board_id,
                str(audit.get("actor_id")) == actor_id,
                str(audit.get("operation")) == OPERATION,
                str(audit.get("manifest_ref")) == str(receipt.get("manifest_ref")),
                str(audit.get("confirmation_ref"))
                == str(receipt.get("confirmation_ref")),
                str(audit.get("user_reason")) == str(receipt.get("user_reason")),
                bool(audit.get("finished_at")),
                bool(audit.get("outcome")),
            )
        ),
        "rebuild_confirmation_terminal_audit_conflict",
        run_id,
    )
    if is_rebuild_terminal_audit_frozen(audit):
        if receipt_state == "terminal":
            # Core has already durably archived this successful operation.
            # The next invocation may rotate the marker through a fresh run.
            return None
        _require(
            receipt_state in (None, "authorized"),
            "frozen_rebuild_receipt_state_invalid",
            run_id,
        )
        _require(
            bundle.generation_repository.get_current(board_id)
            == audit.get("current_kg_generation_id"),
            "rebuild_terminal_generation_pointer_conflict",
            run_id,
        )
        return receipt
    if is_rebuild_terminal_audit_resumable(audit):
        _require(
            receipt_state in (None, "authorized"),
            "resumable_rebuild_receipt_state_invalid",
            run_id,
        )
        return receipt
    if is_rebuild_terminal_audit_closed(audit):
        if receipt_state == "terminal":
            # The previous failed operation is fully archived. A new fresh
            # operation may rotate it through Core's atomic receipt CAS.
            return None
        # A crash may have persisted the closed audit before archiving active.
        # Resume once so Core archives that exact operation; the failed outcome
        # is surfaced and no fresh mutation starts in this invocation.
        return receipt
    raise RecoveryRefused(f"rebuild_confirmation_audit_unclassified:{run_id}")


def _select_recovery_run_plan(
    bundle: ServiceBundle,
    *,
    board_id: str,
    actor_id: str,
    raw_health: Mapping[str, Any],
) -> RecoveryRunPlan:
    receipt = _discover_incomplete_receipt(
        bundle,
        board_id=board_id,
        actor_id=actor_id,
    )
    if receipt is None:
        from okto_pulse.core.kg.rebuild_service import (
            RebuildConfirmationReceiptIntegrityError,
            is_rebuild_terminal_audit_closed,
            load_verified_rebuild_confirmation_receipt,
        )

        try:
            previous_terminal_receipt = load_verified_rebuild_confirmation_receipt(
                artifact_store=bundle.artifact_store,
                board_id=board_id,
            )
        except RebuildConfirmationReceiptIntegrityError as exc:
            raise RecoveryRefused(
                "previous_rebuild_confirmation_receipt_integrity_invalid"
            ) from exc
        previous_terminal_audit: Mapping[str, Any] | None = None
        if previous_terminal_receipt is not None:
            _require(
                previous_terminal_receipt.get("receipt_state") == "terminal"
                and str(previous_terminal_receipt.get("actor_id")) == actor_id
                and str(previous_terminal_receipt.get("operation")) == OPERATION,
                "previous_rebuild_terminal_receipt_invalid",
            )
            previous_terminal_audit = _read_run_audit(
                bundle,
                board_id=board_id,
                run_id=str(previous_terminal_receipt["run_id"]),
            )
            _require(
                is_rebuild_terminal_audit_closed(previous_terminal_audit),
                "previous_rebuild_terminal_audit_invalid",
            )
            # Do not build/write a new manifest until the caller has proved
            # the prior closed operation was either effect-free or completely
            # compensated.  This also makes a crash between bounded lanes
            # safely restartable without creating orphan fresh artifacts.
            return RecoveryRunPlan(
                mode="fresh_pending_terminal_proof",
                manifest=None,
                source_set=None,
                preflight_hash="",
                previous_terminal_receipt=previous_terminal_receipt,
                previous_terminal_audit=previous_terminal_audit,
            )
        preflight, manifest, source_set = _create_preflight_and_manifest(
            bundle,
            board_id=board_id,
            raw_health=raw_health,
        )
        return RecoveryRunPlan(
            mode="fresh",
            manifest=manifest,
            source_set=source_set,
            preflight_hash=str(preflight.preflight_hash),
        )

    manifest_ref = str(receipt["manifest_ref"])
    preflight_hash = str(receipt["preflight_hash"])
    from okto_pulse.core.kg.rebuild_service import (
        is_rebuild_terminal_audit_closed,
        is_rebuild_terminal_audit_frozen,
        is_rebuild_terminal_audit_resumable,
    )

    run_audit = _read_run_audit(
        bundle,
        board_id=board_id,
        run_id=str(receipt["run_id"]),
    )
    frozen_terminal = is_rebuild_terminal_audit_frozen(run_audit)
    closed_terminal = is_rebuild_terminal_audit_closed(run_audit)
    closed_receipt_only = bool(
        closed_terminal
        and not frozen_terminal
        and run_audit is not None
        and str(run_audit.get("outcome") or "") != "completed"
        and not any(
            (
                run_audit.get("report_ref"),
                run_audit.get("report_id"),
                run_audit.get("current_kg_generation_id"),
                run_audit.get("event_emitted") is True,
                run_audit.get("promotion_outcome") == "promoted",
                run_audit.get("publishable_status") == "completed",
            )
        )
    )
    resumable_terminal_evidence = bool(
        is_rebuild_terminal_audit_resumable(run_audit)
        and run_audit is not None
        and any(
            (
                run_audit.get("report_ref"),
                run_audit.get("current_kg_generation_id"),
                run_audit.get("event_emitted") is True,
            )
        )
    )
    from okto_pulse.core.kg.rebuild_sources import (
        RebuildSourceManifestVerificationError,
        SourceSetRevalidation,
    )

    manifest: Any | None = None
    current_source_set: Any | None = None
    rebaseline_evidence: Mapping[str, Any] | None = None
    verification_failure: str | None = None
    try:
        current_source_set = bundle.source_enumerator.enumerate(board_id=board_id)
        manifest = bundle.manifest_store.load_verified(
            manifest_ref,
            expected_board_id=board_id,
            expected_preflight_hash=preflight_hash,
            cognitive_digest=current_source_set.cognitive_durable_digest,
        )
        if str(manifest.source_set_hash) != str(receipt["source_set_hash"]):
            verification_failure = "authorized_resume_source_set_hash_mismatch"
        else:
            revalidation = bundle.manifest_store.classify_revalidation(
                manifest=manifest,
                current_source_set=current_source_set,
            )
            if revalidation.outcome is SourceSetRevalidation.MANIFEST_DRIFT:
                verification_failure = f"source_set_{revalidation.outcome.value}"
            elif revalidation.outcome is SourceSetRevalidation.REBASELINE:
                rebaseline_evidence = dict(revalidation.to_dict())
    except RebuildSourceManifestVerificationError as exc:
        verification_failure = type(exc).__name__
    except RecoveryRefused:
        raise
    except Exception as exc:
        # Core owns the compensation decision for a receipt-authorized resume.
        # Preserve only a bounded classification; never trust a partial object.
        verification_failure = f"source_verification_{type(exc).__name__}"

    if verification_failure is not None:
        if closed_receipt_only:
            # A closed, failed, non-promoted audit is already the authority for
            # this operation.  Core's resume path only archives its exact
            # receipt; it neither trusts the old manifest nor touches graph or
            # queue state.  A fresh lane will enumerate current sources after
            # that receipt-only reconciliation has been proved and fenced.
            return RecoveryRunPlan(
                mode="archive_closed",
                manifest=AuthorizedManifestBinding(
                    board_id=board_id,
                    manifest_ref=manifest_ref,
                    source_set_hash=str(receipt["source_set_hash"]),
                ),
                source_set=None,
                preflight_hash=preflight_hash,
                run_id=str(receipt["run_id"]),
                confirmation_ref=str(receipt["confirmation_ref"]),
                user_reason=str(receipt["user_reason"]),
                receipt=receipt,
                terminal_audit=run_audit,
                manifest_verification_failure=verification_failure,
            )
        if verification_failure == "authorized_resume_source_set_hash_mismatch":
            raise RecoveryRefused(
                "terminal_reconciliation_required:"
                f"{verification_failure}:run={receipt['run_id']}"
            )
        if frozen_terminal or closed_terminal or resumable_terminal_evidence:
            # Terminal/irreversible evidence always outranks manifest
            # compensation. Without an equivalent verified manifest the
            # receipt must remain authorized for explicit reconciliation; do
            # not let Core archive it or compensate an already-visible effect.
            raise RecoveryRefused(
                "terminal_reconciliation_required:"
                f"{verification_failure}:run={receipt['run_id']}"
            )
        return RecoveryRunPlan(
            mode="resume_compensation",
            manifest=AuthorizedManifestBinding(
                board_id=board_id,
                manifest_ref=manifest_ref,
                source_set_hash=str(receipt["source_set_hash"]),
            ),
            source_set=None,
            preflight_hash=preflight_hash,
            run_id=str(receipt["run_id"]),
            confirmation_ref=str(receipt["confirmation_ref"]),
            user_reason=str(receipt["user_reason"]),
            receipt=receipt,
            terminal_audit=run_audit,
            manifest_verification_failure=verification_failure,
        )
    _require(
        manifest is not None and current_source_set is not None,
        "authorized_resume_manifest_verification_incomplete",
    )
    if closed_terminal and not frozen_terminal and not closed_receipt_only:
        raise RecoveryRefused(
            "terminal_reconciliation_required:"
            f"closed_terminal_effects:run={receipt['run_id']}"
        )
    return RecoveryRunPlan(
        mode="archive_closed" if closed_receipt_only else "resume",
        manifest=manifest,
        source_set=current_source_set,
        preflight_hash=preflight_hash,
        run_id=str(receipt["run_id"]),
        confirmation_ref=str(receipt["confirmation_ref"]),
        user_reason=str(receipt["user_reason"]),
        receipt=receipt,
        terminal_audit=run_audit,
        frozen_terminal=frozen_terminal,
        rebaseline_evidence=rebaseline_evidence,
    )


def _load_checkpoint(bundle: ServiceBundle, board_id: str, manifest_ref: str) -> Any:
    from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects
    from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey

    run_id = f"f06:{manifest_ref}"
    checkpoint_id = CommunityRebuildEffects._checkpoint_id(run_id)
    return bundle.artifact_store.read_json(
        RebuildAuditKey(
            namespace="run_audit",
            board_id=board_id,
            artifact_id=checkpoint_id,
        )
    )


def _compensated_queue_adoption_from_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    board_id: str,
    manifest_ref: str,
) -> CompensatedQueueAdoption | None:
    """Derive the exact v4 tombstone authority from a compensated checkpoint."""

    rows = _resume_checkpoint_source_rows(
        checkpoint,
        board_id=board_id,
        manifest_ref=manifest_ref,
    )
    receipts = checkpoint.get("receipts")
    effect_key = f"f06:{manifest_ref}:compensate"
    effect = receipts.get(effect_key) if isinstance(receipts, Mapping) else None
    if effect is None:
        _require(
            not tuple(checkpoint.get("compensation_actions") or ()),
            "compensated_queue_effect_receipt_missing",
        )
        return None
    _require(
        isinstance(effect, Mapping)
        and effect.get("ok") is True
        and str(effect.get("effect")) == "compensate"
        and str(effect.get("code")) == "compensated",
        "compensated_queue_effect_receipt_invalid",
    )
    identities = _expected_queue_order(rows)
    membership_by_identity = {
        identity: {
            "run_id": manifest_ref,
            "source_ref": str(row.get("source_ref", row.get("id", ""))),
            "source_version": str(row.get("source_version") or ""),
            "content_hash": str(row.get("content_hash") or ""),
        }
        for identity, row in zip(identities, rows, strict=True)
    }
    return CompensatedQueueAdoption(
        source=f"rebuild:{manifest_ref}",
        manifest_ref=manifest_ref,
        identities=frozenset(identities),
        membership_by_identity=membership_by_identity,
    )


def _closed_operation_effect_paths(manifest_ref: str) -> frozenset[str]:
    from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects

    run_id = f"f06:{manifest_ref}"
    keys = (
        *(f"{run_id}:{effect}" for effect in ("snapshot", "quarantine", "enqueue")),
        *(f"{run_id}:{effect}" for effect in ("restore", "promote", "compensate")),
        f"{run_id}:audit:manifest_drift",
    )
    return frozenset(
        {
            f"audit/{CommunityRebuildEffects._checkpoint_id(run_id)}.json",
            *(
                f"audit/{CommunityRebuildEffects._effect_id(effect_key)}.json"
                for effect_key in keys
            ),
        }
    )


def _closed_operation_has_orphan_effect_evidence(
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    board_id: str,
    manifest_ref: str,
    run_id: str,
    candidate_generation_id: str | None,
) -> bool:
    """Detect run-bound evidence that cannot be called receipt-only."""

    identities = frozenset(
        value for value in (manifest_ref, run_id, candidate_generation_id) if value
    )
    allowed = {
        f"audit/{run_id}.json",
        f"audit/confirmation_receipts/{board_id}/active.json",
        f"audit/confirmation_receipts/{board_id}/{run_id}.json",
        f"manifests/{manifest_ref}.json",
    }
    for relative in sorted(rebuild_baseline):
        if relative in allowed or not relative.endswith(".json"):
            continue
        # The confirmation-consumption row is observability emitted before
        # the processor; it is not evidence that graph/queue mutation began.
        if relative.startswith(f"audit/confirmation/{board_id}/"):
            continue
        path = rebuild_root / relative
        payload = _load_json_file(path, "closed_operation_artifact_invalid")
        if _payload_mentions_any(payload, identities):
            return True
    for relative in sorted(quarantine_baseline):
        if Path(relative).name != "manifest.json":
            continue
        payload = _load_json_file(
            quarantine_root / relative,
            "closed_operation_quarantine_manifest_invalid",
        )
        if _payload_mentions_any(payload, identities):
            return True
    return False


def _assert_closed_operation_baseline_safe(
    bundle: ServiceBundle,
    *,
    receipt: Mapping[str, Any],
    audit: Mapping[str, Any],
    board_id: str,
    db_path: Path,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    board_storage_root: Path,
    raw_health: Mapping[str, Any],
) -> tuple[str, CompensatedQueueAdoption | None]:
    """Prove a closed failure is effect-free or completely compensated."""

    from okto_pulse.core.kg.rebuild_service import (
        ClosedRebuildReconciliation,
        classify_closed_rebuild_reconciliation,
        is_rebuild_terminal_audit_closed,
        is_rebuild_terminal_audit_frozen,
    )

    run_id = str(receipt.get("run_id") or "")
    manifest_ref = str(receipt.get("manifest_ref") or "")
    _require(
        bool(run_id)
        and bool(manifest_ref)
        and receipt.get("receipt_state") in (None, "authorized", "terminal")
        and is_rebuild_terminal_audit_closed(audit)
        and not is_rebuild_terminal_audit_frozen(audit)
        and str(audit.get("run_id") or "") == run_id
        and str(audit.get("board_id") or "") == board_id
        and str(audit.get("manifest_ref") or "") == manifest_ref
        and not any(
            (
                audit.get("report_ref"),
                audit.get("report_id"),
                audit.get("current_kg_generation_id"),
                audit.get("event_emitted") is True,
                audit.get("promotion_outcome") == "promoted",
                audit.get("publishable_status") == "completed",
            )
        ),
        "terminal_reconciliation_required:closed_audit_not_reconcilable",
        run_id,
    )
    checkpoint = _load_checkpoint(bundle, board_id, manifest_ref)
    reconciliation = classify_closed_rebuild_reconciliation(
        receipt=receipt,
        audit=audit,
        checkpoint=checkpoint,
    )
    _require(
        reconciliation is not ClosedRebuildReconciliation.AMBIGUOUS,
        "terminal_reconciliation_required:closed_core_classification_ambiguous",
        run_id,
    )
    source = f"rebuild:{manifest_ref}"
    if reconciliation is ClosedRebuildReconciliation.RECEIPT_ONLY:
        _require(checkpoint is None, "closed_core_receipt_only_checkpoint_conflict")
        _require(
            not (_closed_operation_effect_paths(manifest_ref) & set(rebuild_baseline)),
            "terminal_reconciliation_required:closed_effect_artifact_without_checkpoint",
            run_id,
        )
        _require(
            not tuple(audit.get("affected_files") or ()),
            "terminal_reconciliation_required:closed_affected_files_without_checkpoint",
            run_id,
        )
        _require(
            not _exact_queue_rows(db_path, board_id=board_id, source=source).rows,
            "terminal_reconciliation_required:closed_queue_without_checkpoint",
            run_id,
        )
        candidate = receipt.get("candidate_kg_generation_id")
        _require(
            not _closed_operation_has_orphan_effect_evidence(
                rebuild_root=rebuild_root,
                rebuild_baseline=rebuild_baseline,
                quarantine_root=quarantine_root,
                quarantine_baseline=quarantine_baseline,
                board_id=board_id,
                manifest_ref=manifest_ref,
                run_id=run_id,
                candidate_generation_id=(str(candidate) if candidate else None),
            ),
            "terminal_reconciliation_required:closed_orphan_effect_evidence",
            run_id,
        )
        previous_generation = receipt.get("previous_kg_generation_id")
        if previous_generation is not None:
            _require(
                str(raw_health.get("current_kg_generation_id") or "")
                == str(previous_generation),
                "terminal_reconciliation_required:closed_generation_pointer_drift",
                run_id,
            )
        return "receipt_only", None

    _require(
        reconciliation is ClosedRebuildReconciliation.FULLY_COMPENSATED
        and isinstance(checkpoint, Mapping)
        and _checkpoint_state(checkpoint) == "failed",
        "terminal_reconciliation_required:closed_checkpoint_not_compensated",
        run_id,
    )
    assert isinstance(checkpoint, Mapping)
    command = checkpoint.get("command")
    _require(
        isinstance(command, Mapping)
        and str(command.get("run_id") or "") == f"f06:{manifest_ref}"
        and str(command.get("board_id") or "") == board_id
        and str(command.get("manifest_ref") or "") == manifest_ref,
        "terminal_reconciliation_required:closed_checkpoint_binding_invalid",
        run_id,
    )
    failed_state = str(checkpoint.get("compensation_failed_state") or "")
    expected_effects_by_state = {
        "quarantined": ("snapshot", "quarantine"),
        "enqueued": ("snapshot", "quarantine", "enqueue"),
        "draining": ("snapshot", "quarantine", "enqueue"),
        "restored": ("snapshot", "quarantine", "enqueue", "restore"),
        "promoted": (
            "snapshot",
            "quarantine",
            "enqueue",
            "restore",
            "promote",
        ),
        "completed": (
            "snapshot",
            "quarantine",
            "enqueue",
            "restore",
            "promote",
        ),
    }
    expected_actions_by_state = {
        "quarantined": ("restore_quarantine", "discard_candidate_generation"),
        "enqueued": (
            "cancel_enqueued_sources",
            "restore_quarantine",
            "discard_candidate_generation",
        ),
        "draining": (
            "cancel_enqueued_sources",
            "restore_quarantine",
            "discard_candidate_generation",
        ),
        "restored": (
            "cancel_enqueued_sources",
            "restore_quarantine",
            "discard_candidate_generation",
        ),
        "promoted": (
            "cancel_enqueued_sources",
            "demote_candidate_generation",
            "restore_quarantine",
            "discard_candidate_generation",
        ),
        "completed": (
            "cancel_enqueued_sources",
            "demote_candidate_generation",
            "restore_quarantine",
            "discard_candidate_generation",
        ),
    }
    _require(
        failed_state in expected_effects_by_state
        and tuple(checkpoint.get("compensation_actions") or ())
        == expected_actions_by_state[failed_state],
        "terminal_reconciliation_required:closed_compensation_plan_invalid",
        run_id,
    )
    adoption = _compensated_queue_adoption_from_checkpoint(
        checkpoint,
        board_id=board_id,
        manifest_ref=manifest_ref,
    )
    _require(
        adoption is not None,
        "terminal_reconciliation_required:closed_compensation_receipt_missing",
        run_id,
    )
    assert adoption is not None
    receipts = checkpoint.get("receipts")
    _require(isinstance(receipts, Mapping), "closed_compensation_receipts_invalid")
    assert isinstance(receipts, Mapping)
    _require(
        all(
            f"f06:{manifest_ref}:{effect}" in receipts
            and isinstance(receipts[f"f06:{manifest_ref}:{effect}"], Mapping)
            and receipts[f"f06:{manifest_ref}:{effect}"].get("ok") is True
            and str(receipts[f"f06:{manifest_ref}:{effect}"].get("effect") or "")
            == effect
            for effect in expected_effects_by_state[failed_state]
        ),
        "terminal_reconciliation_required:closed_prerequisite_effect_missing",
        run_id,
    )
    for effect_key, raw_receipt in receipts.items():
        _require(
            isinstance(effect_key, str)
            and effect_key.startswith(f"f06:{manifest_ref}:")
            and isinstance(raw_receipt, Mapping)
            and str(raw_receipt.get("effect_key") or "") == effect_key,
            "terminal_reconciliation_required:closed_effect_binding_invalid",
            run_id,
        )
        from okto_pulse.community.adapters.rebuild_effects import (
            CommunityRebuildEffects,
        )

        relative = f"audit/{CommunityRebuildEffects._effect_id(effect_key)}.json"
        _require(
            relative in rebuild_baseline
            and _load_json_file(
                rebuild_root / relative,
                "closed_compensation_effect_artifact_invalid",
            )
            == dict(raw_receipt),
            "terminal_reconciliation_required:closed_effect_artifact_mismatch",
            effect_key,
        )
    compensate_key = f"f06:{manifest_ref}:compensate"
    compensate_receipt = receipts.get(compensate_key)
    assert isinstance(compensate_receipt, Mapping)
    details = compensate_receipt.get("details")
    queue_details = details.get("queue") if isinstance(details, Mapping) else None
    compensation_actions = tuple(checkpoint.get("compensation_actions") or ())
    _require(
        isinstance(details, Mapping)
        and tuple(details.get("actions") or ()) == compensation_actions,
        "terminal_reconciliation_required:closed_compensation_details_invalid",
        run_id,
    )
    if "cancel_enqueued_sources" in compensation_actions:
        _require(
            isinstance(queue_details, Mapping)
            and int(queue_details.get("active_remaining", -1)) == 0
            and int(queue_details.get("live_intents_restored", -1)) == 0,
            "terminal_reconciliation_required:closed_compensation_queue_invalid",
            run_id,
        )
    else:
        _require(
            queue_details is None,
            "terminal_reconciliation_required:closed_compensation_queue_unexpected",
            run_id,
        )
    restore_details = details.get("quarantine_restore")
    discard_details = details.get("candidate_discard")
    _require(
        isinstance(restore_details, Mapping)
        and restore_details.get("ok") is True
        and isinstance(discard_details, Mapping)
        and str(discard_details.get("status") or "")
        in {
            "already_absent",
            "discarded",
            "discarded_by_atomic_backup_swap",
        },
        "terminal_reconciliation_required:closed_compensation_storage_receipt_invalid",
        run_id,
    )
    _require(
        _active_exact_queue_depth(db_path, board_id=board_id, source=source) == 0,
        "terminal_reconciliation_required:closed_compensation_queue_active",
        run_id,
    )
    _assert_protected_admission_is_noop(
        db_path,
        board_id=board_id,
        admitted_identities=set(adoption.identities),
        authorized_rebuild_source="__closed_compensation_no_active_source__",
        compensated_adoption=adoption,
    )
    expected_board_storage = _expected_compensation_board_storage(
        checkpoint,
        quarantine_root=quarantine_root,
        board_id=board_id,
        manifest_ref=manifest_ref,
    )
    _require(
        _snapshot_tree_hashes(board_storage_root) == expected_board_storage,
        "terminal_reconciliation_required:closed_compensation_graph_mismatch",
        run_id,
    )
    previous_generation = command.get("previous_generation_id")
    _require(
        raw_health.get("current_kg_generation_id") == previous_generation,
        "terminal_reconciliation_required:closed_compensation_pointer_mismatch",
        run_id,
    )
    if expected_board_storage:
        _require(
            bool(raw_health.get("graph_storage_exists")),
            "terminal_reconciliation_required:closed_compensation_graph_unresolved",
            run_id,
        )
    else:
        _require(
            previous_generation is None,
            "terminal_reconciliation_required:closed_compensation_graph_absent",
            run_id,
        )
    return "fully_compensated", adoption


def _resume_checkpoint_source_rows(
    checkpoint: Mapping[str, Any] | None,
    *,
    board_id: str,
    manifest_ref: str,
) -> tuple[Mapping[str, Any], ...]:
    """Load only an exactly-bound persisted command for compensation fencing."""

    if checkpoint is None:
        return ()
    command = checkpoint.get("command")
    _require(
        isinstance(command, Mapping), "authorized_resume_checkpoint_command_missing"
    )
    assert isinstance(command, Mapping)
    _require(
        str(command.get("board_id")) == board_id
        and str(command.get("manifest_ref")) == manifest_ref
        and str(command.get("operation")) == OPERATION,
        "authorized_resume_checkpoint_binding_mismatch",
    )
    raw_rows = command.get("source_rows")
    _require(
        isinstance(raw_rows, list) and bool(raw_rows),
        "authorized_resume_checkpoint_source_rows_invalid",
    )
    assert isinstance(raw_rows, list)
    rows = tuple(dict(row) for row in raw_rows if isinstance(row, Mapping))
    _require(
        len(rows) == len(raw_rows),
        "authorized_resume_checkpoint_source_rows_invalid",
    )
    _expected_queue_order(rows)
    return rows


def _checkpoint_state(checkpoint: Mapping[str, Any]) -> str:
    return str(checkpoint.get("state") or "")


def _assert_source_unchanged(
    bundle: ServiceBundle,
    manifest: Any,
    board_id: str,
    *,
    require_terminal_binding: bool = False,
    expected_rebaseline_target_source_set_hash: str | None = None,
) -> None:
    from okto_pulse.core.kg.rebuild_sources import SourceSetRevalidation

    current = bundle.source_enumerator.enumerate(board_id=board_id)
    result = bundle.manifest_store.classify_revalidation(
        manifest=manifest,
        current_source_set=current,
    )
    _require(
        result.outcome
        in {SourceSetRevalidation.EQUIVALENT, SourceSetRevalidation.REBASELINE},
        "source_set_changed",
        str(result.outcome.value),
    )
    if not require_terminal_binding:
        return
    if expected_rebaseline_target_source_set_hash is None:
        _require(
            result.outcome is SourceSetRevalidation.EQUIVALENT,
            "terminal_source_set_rebaseline_unbound",
            str(result.outcome.value),
        )
        return
    _require(
        re.fullmatch(r"[0-9a-f]{64}", expected_rebaseline_target_source_set_hash)
        is not None,
        "terminal_rebaseline_target_source_set_hash_invalid",
    )
    _require(
        result.outcome is SourceSetRevalidation.REBASELINE
        and result.to_source_set_hash == expected_rebaseline_target_source_set_hash,
        "terminal_rebaseline_target_source_set_changed",
        (
            f"expected={expected_rebaseline_target_source_set_hash} "
            f"actual={result.to_source_set_hash}"
        ),
    )


def _resolve_expected_command_sources(
    db_path: Path,
    *,
    board_id: str,
    manifest: Any,
    rebaseline_source_set: Any | None = None,
    rebaseline_evidence_id: str | None = None,
) -> tuple[tuple[Mapping[str, Any], ...], int]:
    """Use the production v4 dependency-closure resolver before admission."""

    from okto_pulse.community.adapters.board_rebuild_ingestion import (
        _EVIDENCE_CLOSURE_CANDIDATE,
        _resolve_evidence_dependency_closure,
    )

    _require(
        (rebaseline_source_set is None) == (rebaseline_evidence_id is None),
        "rebaseline_projection_binding_incomplete",
    )
    if rebaseline_source_set is not None:
        _require(
            str(getattr(rebaseline_source_set, "board_id", "")) == board_id,
            "rebaseline_projection_board_mismatch",
        )
        _require(bool(rebaseline_evidence_id), "rebaseline_projection_evidence_invalid")
        source_projection = rebaseline_source_set
    else:
        source_projection = manifest

    manifest_sources_list: list[dict[str, Any]] = []
    for row in tuple(source_projection.materializable_sources):
        payload = row.to_dict()
        payload["_rebuild_manifest_created_at"] = manifest.created_at
        if rebaseline_evidence_id is not None:
            payload["_rebuild_rebaseline_evidence_id"] = rebaseline_evidence_id
        manifest_sources_list.append(payload)
    for row in tuple(source_projection.skipped_expired_working):
        if (
            row.artifact_type != "code_evidence"
            or row.source_artifact_status != "superseded"
        ):
            continue
        payload = row.to_dict()
        payload["_rebuild_manifest_created_at"] = manifest.created_at
        payload["_rebuild_dependency_closure_candidate"] = _EVIDENCE_CLOSURE_CANDIDATE
        if rebaseline_evidence_id is not None:
            payload["_rebuild_rebaseline_evidence_id"] = rebaseline_evidence_id
        manifest_sources_list.append(payload)
    manifest_sources = tuple(manifest_sources_list)
    resolved, closure_count = _resolve_evidence_dependency_closure(
        db_path=db_path,
        board_id=board_id,
        sources=manifest_sources,
    )
    source_rows = tuple(dict(row) for row in resolved if isinstance(row, Mapping))
    _require(len(source_rows) == len(resolved), "dependency_closure_rows_invalid")
    _require(bool(source_rows), "dependency_closure_empty")
    _require(int(closure_count) >= 0, "dependency_closure_count_invalid")
    _expected_queue_order(source_rows)
    return source_rows, int(closure_count)


def _production_ordered_source_rows(
    db_path: Path,
    *,
    board_id: str,
    source_rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    from okto_pulse.community.adapters.board_rebuild_ingestion import (
        _ordered_rebuild_sources,
    )

    with _sqlite_connect_readonly(db_path) as connection:
        ordered = _ordered_rebuild_sources(
            connection,
            board_id=board_id,
            sources=source_rows,
        )
    result = tuple(dict(row) for row in ordered if isinstance(row, Mapping))
    _require(len(result) == len(source_rows), "production_source_order_invalid")
    _require(
        _expected_queue_keys(result) == _expected_queue_keys(source_rows),
        "production_source_order_identity_drift",
    )
    return result


def _canonical_source_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(
        [dict(row) for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _assert_reservation_exact(
    bundle: ServiceBundle,
    *,
    board_id: str,
    manifest_ref: str,
) -> None:
    reservation = bundle.operation_reservation.inspect(board_id=board_id)
    _require(reservation is not None, "administrative_reservation_missing")
    assert reservation is not None
    _require(reservation.admin_lane, "administrative_reservation_not_admin")
    _require(
        reservation.operation == f"kg02_rebuild_reservation:{manifest_ref}",
        "administrative_reservation_mismatch",
        reservation.operation,
    )
    _require(
        reservation.expires_at_epoch > time.time(),
        "administrative_reservation_expired",
    )


def _expected_queue_order(
    source_rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    from okto_pulse.core.kg.board_rebuild_adapter import (
        DETERMINISTIC_SOURCE_ARTIFACT_TYPES,
        queue_artifact_type,
    )

    expected: list[tuple[str, str]] = []
    for source in source_rows:
        artifact_type = str(source.get("artifact_type") or "")
        artifact_id = str(source.get("id") or "")
        _require(
            artifact_type in DETERMINISTIC_SOURCE_ARTIFACT_TYPES and bool(artifact_id),
            "checkpoint_source_not_materializable",
            f"{artifact_type}:{artifact_id}",
        )
        expected.append((queue_artifact_type(artifact_type), artifact_id))
    _require(
        len(set(expected)) == len(source_rows),
        "checkpoint_source_identity_duplicate",
    )
    return tuple(expected)


def _expected_queue_keys(
    source_rows: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    return set(_expected_queue_order(source_rows))


def _parse_queue_timestamp(raw: Any) -> datetime:
    value = str(raw or "").strip()
    _require(bool(value), "rebuild_queue_triggered_at_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryRefused("rebuild_queue_triggered_at_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_admission_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    board_id: str,
    manifest_ref: str,
    initial_board_dlq_ids: Sequence[str],
    require_draining: bool,
) -> tuple[Mapping[str, Any], ...]:
    from okto_pulse.community.adapters.board_rebuild_ingestion import (
        REBUILD_QUEUE_ORDER_VERSION,
    )

    checkpoint_state = _checkpoint_state(checkpoint)
    if require_draining:
        _require(
            checkpoint_state == CHECKPOINT_STATE_DRAINING,
            "checkpoint_not_draining",
            checkpoint_state,
        )
    else:
        _require(
            checkpoint_state in POST_DRAIN_CHECKPOINT_STATES,
            "checkpoint_not_post_drain",
            checkpoint_state,
        )
    _require(
        int(checkpoint.get("writer_handoff_count", 0)) == 1,
        "checkpoint_writer_handoff_invalid",
    )
    _require(
        int(checkpoint.get("writer_reacquire_count", 0))
        == (0 if require_draining else 1),
        (
            "checkpoint_writer_reacquire_early"
            if require_draining
            else "checkpoint_writer_reacquire_missing"
        ),
    )
    command = checkpoint.get("command")
    _require(isinstance(command, Mapping), "checkpoint_command_missing")
    assert isinstance(command, Mapping)
    _require(str(command.get("board_id")) == board_id, "checkpoint_board_mismatch")
    _require(
        str(command.get("manifest_ref")) == manifest_ref,
        "checkpoint_manifest_mismatch",
    )
    source_rows_value = command.get("source_rows")
    _require(isinstance(source_rows_value, list), "checkpoint_source_rows_missing")
    source_rows = tuple(
        dict(row) for row in source_rows_value if isinstance(row, Mapping)
    )
    _require(
        len(source_rows) == len(source_rows_value), "checkpoint_source_rows_invalid"
    )
    _require(bool(source_rows), "checkpoint_source_rows_empty")

    receipts = checkpoint.get("receipts")
    _require(isinstance(receipts, Mapping), "checkpoint_receipts_missing")
    enqueue = dict(receipts).get(f"f06:{manifest_ref}:enqueue")
    _require(isinstance(enqueue, Mapping), "checkpoint_enqueue_receipt_missing")
    assert isinstance(enqueue, Mapping)
    _require(bool(enqueue.get("ok")), "checkpoint_enqueue_receipt_failed")
    _require(
        str(enqueue.get("effect")) == "enqueue", "checkpoint_enqueue_effect_invalid"
    )
    details = enqueue.get("details")
    _require(isinstance(details, Mapping), "checkpoint_enqueue_details_missing")
    assert isinstance(details, Mapping)
    _require(
        int(details.get("queue_order_version", 0)) == int(REBUILD_QUEUE_ORDER_VERSION)
        and int(REBUILD_QUEUE_ORDER_VERSION) >= 4,
        "checkpoint_enqueue_order_not_v4",
    )
    _require(
        bool(details.get("enqueue_admission_complete", False)),
        "checkpoint_enqueue_admission_incomplete",
    )
    baseline = tuple(
        str(value) for value in details.get("baseline_dead_letter_ids", ())
    )
    _require(
        baseline == tuple(initial_board_dlq_ids),
        "checkpoint_dlq_baseline_mismatch",
    )
    return source_rows


def _assert_exact_queue_admission(
    snapshot: QueueSnapshot,
    *,
    manifest_ref: str,
    source_rows: Sequence[Mapping[str, Any]],
    ordered_source_rows: Sequence[Mapping[str, Any]],
    allow_consumed_prefix: bool = False,
    allow_claimed_recovery: bool = False,
) -> int:
    expected_order = _expected_queue_order(ordered_source_rows)
    command_order = _expected_queue_order(source_rows)
    _require(
        set(expected_order) == set(command_order),
        "rebuild_queue_order_source_identity_drift",
    )
    expected_by_identity = {
        identity: source_row
        for identity, source_row in zip(command_order, source_rows, strict=True)
    }
    observed_order: list[tuple[str, str]] = []
    previous_triggered_at: datetime | None = None
    claimed_count = 0
    for row in snapshot.rows:
        (
            _row_id,
            artifact_type,
            artifact_id,
            status,
            priority,
            attempts,
            last_error,
            next_retry_at,
            claimed_at,
            claim_timeout_at,
            worker_id,
            claimed_by_session_id,
            claim_token,
            generation,
            delete_event_id,
            work_kind,
            source,
            triggered_at,
            payload,
        ) = row
        identity = (str(artifact_type), str(artifact_id))
        observed_order.append(identity)
        _require(
            observed_order.count(identity) == 1,
            "rebuild_queue_identity_duplicate",
            f"{identity[0]}:{identity[1]}",
        )
        parsed_triggered_at = _parse_queue_timestamp(triggered_at)
        _require(
            previous_triggered_at is None
            or parsed_triggered_at > previous_triggered_at,
            "rebuild_queue_triggered_at_not_strictly_increasing",
        )
        previous_triggered_at = parsed_triggered_at
        status_text = str(status)
        _require(
            type(generation) is int and generation >= 0,
            "rebuild_queue_generation_invalid",
            str(_row_id),
        )
        _require(
            delete_event_id is None
            or (isinstance(delete_event_id, str) and bool(delete_event_id)),
            "rebuild_queue_delete_event_id_invalid",
            str(_row_id),
        )
        _require(
            status_text == "pending"
            or (allow_claimed_recovery and status_text == "claimed"),
            "rebuild_queue_row_status_invalid",
            str(_row_id),
        )
        _require(
            str(priority) == "high", "rebuild_queue_priority_invalid", str(_row_id)
        )
        _require(int(attempts) == 0, "rebuild_queue_attempts_nonzero", str(_row_id))
        if status_text == "claimed":
            claimed_count += 1
            _require(
                last_error is None
                and next_retry_at is None
                and all(
                    isinstance(value, str) and bool(value)
                    for value in (
                        claimed_at,
                        claim_timeout_at,
                        worker_id,
                        claimed_by_session_id,
                        claim_token,
                    )
                ),
                "rebuild_queue_claim_recovery_identity_invalid",
                str(_row_id),
            )
            _require(
                str(worker_id) == str(claimed_by_session_id)
                and re.fullmatch(r"[0-9a-f]{32}", str(claim_token)) is not None
                and _parse_queue_timestamp(claim_timeout_at)
                > _parse_queue_timestamp(claimed_at),
                "rebuild_queue_claim_recovery_fence_invalid",
                str(_row_id),
            )
        else:
            _require(
                all(
                    value is None
                    for value in (
                        last_error,
                        next_retry_at,
                        claimed_at,
                        claim_timeout_at,
                        worker_id,
                        claimed_by_session_id,
                        claim_token,
                    )
                ),
                "rebuild_queue_claim_or_error_present",
                str(_row_id),
            )
        _require(str(work_kind) == "consolidate", "rebuild_queue_work_kind_invalid")
        _require(
            str(source) == f"rebuild:{manifest_ref}", "rebuild_queue_source_invalid"
        )
        try:
            decoded = json.loads(str(payload)) if isinstance(payload, str) else payload
        except json.JSONDecodeError as exc:
            raise RecoveryRefused("rebuild_queue_payload_invalid") from exc
        membership = (
            decoded.get("_rebuild_membership") if isinstance(decoded, Mapping) else None
        )
        _require(isinstance(membership, Mapping), "rebuild_queue_membership_missing")
        _require(
            str(membership.get("run_id")) == manifest_ref,
            "rebuild_queue_membership_run_mismatch",
        )
        expected_source = expected_by_identity.get(identity)
        _require(expected_source is not None, "rebuild_queue_membership_unexpected")
        assert expected_source is not None
        for membership_field, source_field in (
            ("source_ref", "source_ref"),
            ("source_version", "source_version"),
            ("content_hash", "content_hash"),
        ):
            _require(
                str(membership.get(membership_field) or "")
                == str(expected_source.get(source_field) or ""),
                "rebuild_queue_membership_source_mismatch",
                membership_field,
            )
    observed = tuple(observed_order)
    if allow_consumed_prefix:
        suffix_offset = len(expected_order) - len(observed)
        _require(
            suffix_offset >= 0 and observed == expected_order[suffix_offset:],
            "rebuild_queue_not_deterministic_suffix",
        )
    else:
        _require(
            observed == expected_order,
            "rebuild_queue_topological_order_mismatch",
        )
    return claimed_count


async def _recover_exact_claims_for_resume(
    processor: Any,
    claim_scope: Any,
    bundle: ServiceBundle,
    manifest: Any,
    *,
    board_id: str,
    db_path: Path,
    source: str,
    claimed_count: int,
    source_rows: Sequence[Mapping[str, Any]],
    ordered_source_rows: Sequence[Mapping[str, Any]],
    baseline_dlq: QueueSnapshot,
    baseline_non_target: QueueSnapshot,
    admitted_identities: set[tuple[str, str]],
    cancel_event: threading.Event,
    lifetime_probe: Callable[[], bool],
) -> None:
    """Neutrally repend only dead claims from this authorized resume scope."""

    _require(claimed_count > 0, "exact_claim_recovery_count_invalid")
    _require(not cancel_event.is_set(), "exact_claim_recovery_cancelled")
    _require(lifetime_probe(), "exact_claim_recovery_authority_lost")
    _assert_reservation_exact(
        bundle,
        board_id=board_id,
        manifest_ref=manifest.manifest_ref,
    )
    _require(
        bundle.single_writer_lock.inspect(board_id=board_id) is None,
        "writer_a_not_released_before_exact_claim_recovery",
    )
    _assert_source_unchanged(bundle, manifest, board_id)
    _assert_unchanged_snapshot(
        _dlq_snapshot(db_path),
        baseline_dlq,
        "dead_letter_set_changed_before_exact_claim_recovery",
    )
    _assert_unchanged_snapshot(
        _protected_queue_snapshot(
            db_path,
            board_id=board_id,
            admitted_identities=admitted_identities,
        ),
        baseline_non_target,
        "non_target_queue_changed_before_exact_claim_recovery",
    )

    def recovery_authority_probe() -> bool:
        return bool(not cancel_event.is_set() and lifetime_probe())

    recovered = int(
        await processor.recover_exact_claims(
            claim_scope=claim_scope,
            recovery_authority_probe=recovery_authority_probe,
        )
    )
    _require(
        recovered == claimed_count,
        "exact_claim_recovery_count_mismatch",
        f"expected={claimed_count} actual={recovered}",
    )
    _require(recovery_authority_probe(), "exact_claim_recovery_authority_lost")
    _assert_reservation_exact(
        bundle,
        board_id=board_id,
        manifest_ref=manifest.manifest_ref,
    )
    _require(
        bundle.single_writer_lock.inspect(board_id=board_id) is None,
        "writer_a_reappeared_during_exact_claim_recovery",
    )
    post_recovery = _exact_queue_rows(
        db_path,
        board_id=board_id,
        source=source,
    )
    _assert_exact_queue_admission(
        post_recovery,
        manifest_ref=manifest.manifest_ref,
        source_rows=source_rows,
        ordered_source_rows=ordered_source_rows,
        allow_consumed_prefix=True,
        allow_claimed_recovery=False,
    )
    _require(
        all(
            str(row[post_recovery.columns.index("status")]) == "pending"
            for row in post_recovery.rows
        ),
        "exact_claim_recovery_left_claimed_rows",
    )
    _assert_unchanged_snapshot(
        _dlq_snapshot(db_path),
        baseline_dlq,
        "dead_letter_set_changed_during_exact_claim_recovery",
    )
    _assert_unchanged_snapshot(
        _protected_queue_snapshot(
            db_path,
            board_id=board_id,
            admitted_identities=admitted_identities,
        ),
        baseline_non_target,
        "non_target_queue_changed_during_exact_claim_recovery",
    )
    _assert_source_unchanged(bundle, manifest, board_id)
    _emit(
        "exact_claims_recovered",
        board_id=board_id,
        manifest_ref=manifest.manifest_ref,
        recovered=recovered,
    )


def _assert_unchanged_snapshot(
    actual: QueueSnapshot,
    expected: QueueSnapshot,
    code: str,
) -> None:
    _require(
        actual.fingerprint == expected.fingerprint,
        code,
        f"expected={expected.fingerprint} actual={actual.fingerprint}",
    )


def _manifest_artifact_fingerprint(
    bundle: ServiceBundle,
    *,
    manifest_ref: str,
) -> tuple[str, str]:
    from okto_pulse.core.kg.interfaces.rebuild_audit_storage import (
        REBUILD_AUDIT_GLOBAL_BOARD_ID,
        RebuildAuditKey,
    )

    reference = bundle.artifact_store.reference(
        RebuildAuditKey(
            namespace="source_manifest",
            board_id=REBUILD_AUDIT_GLOBAL_BOARD_ID,
            artifact_id=manifest_ref,
        )
    )
    raw_path = Path(reference)
    _require(not raw_path.is_symlink(), "source_manifest_symlink_refused", reference)
    path = raw_path.resolve(strict=True)
    _require(path.is_file(), "source_manifest_artifact_missing", reference)
    return reference, hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_confirmation_receipt(
    bundle: ServiceBundle,
    *,
    board_id: str,
    actor_id: str,
    manifest: Any,
    run_id: str,
    preflight_hash: str,
    confirmation_ref: str,
    user_reason: str,
) -> str:
    from okto_pulse.core.kg.rebuild_service import (
        load_verified_rebuild_confirmation_receipt,
        rebuild_confirmation_receipt_key,
        rebuild_operation_run_id,
    )

    expected_run_id = rebuild_operation_run_id(
        board_id=board_id,
        operation=OPERATION,
        preflight_hash=preflight_hash,
        source_set_hash=str(manifest.source_set_hash),
        manifest_ref=str(manifest.manifest_ref),
    )
    _require(run_id == expected_run_id, "terminal_operation_run_id_mismatch")
    active_receipt = load_verified_rebuild_confirmation_receipt(
        artifact_store=bundle.artifact_store,
        board_id=board_id,
    )
    _require(
        active_receipt is not None,
        "terminal_confirmation_active_receipt_missing",
        run_id,
    )
    assert active_receipt is not None
    key = rebuild_confirmation_receipt_key(board_id=board_id, run_id=run_id)
    receipt = bundle.artifact_store.read_json(key)
    _require(
        receipt == active_receipt,
        "terminal_confirmation_receipt_mismatch",
        run_id,
    )
    expected_binding = {
        "run_id": run_id,
        "board_id": board_id,
        "actor_id": actor_id,
        "operation": OPERATION,
        "preflight_hash": preflight_hash,
        "manifest_ref": str(manifest.manifest_ref),
        "source_set_hash": str(manifest.source_set_hash),
        "confirmation_ref": confirmation_ref,
        "user_reason": user_reason,
        "receipt_state": "terminal",
    }
    _require(
        all(
            active_receipt.get(key_name) == value
            for key_name, value in expected_binding.items()
        ),
        "terminal_confirmation_receipt_binding_mismatch",
        run_id,
    )
    return str(bundle.artifact_store.reference(key))


def _assert_resume_checkpoint_monotonic(
    baseline: Mapping[str, Any] | None,
    terminal: Mapping[str, Any],
    *,
    manifest_ref: str,
) -> None:
    if baseline is None:
        return
    state_rank = {
        "planned": 0,
        "snapshotted": 1,
        "quarantined": 2,
        "enqueued": 3,
        "draining": 4,
        "restored": 5,
        "promoted": 6,
        "completed": 7,
    }
    baseline_state = _checkpoint_state(baseline)
    terminal_state = _checkpoint_state(terminal)
    _require(
        baseline_state in state_rank
        and terminal_state in state_rank
        and state_rank[terminal_state] >= state_rank[baseline_state],
        "resume_checkpoint_state_regressed",
        f"{baseline_state}->{terminal_state}",
    )
    _require(
        _canonical_source_rows((dict(baseline.get("command") or {}),))
        == _canonical_source_rows((dict(terminal.get("command") or {}),)),
        "resume_checkpoint_command_changed",
    )
    for field in (
        "last_sequence",
        "queue_progress_events",
        "writer_handoff_count",
        "writer_reacquire_count",
    ):
        _require(
            int(terminal.get(field, 0)) >= int(baseline.get(field, 0)),
            "resume_checkpoint_counter_regressed",
            field,
        )
    baseline_receipts = baseline.get("receipts")
    terminal_receipts = terminal.get("receipts")
    _require(
        isinstance(baseline_receipts, Mapping)
        and isinstance(terminal_receipts, Mapping),
        "resume_checkpoint_receipts_invalid",
    )
    assert isinstance(baseline_receipts, Mapping)
    assert isinstance(terminal_receipts, Mapping)
    for key, raw_baseline_receipt in baseline_receipts.items():
        _require(
            key in terminal_receipts, "resume_checkpoint_receipt_removed", str(key)
        )
        raw_terminal_receipt = terminal_receipts[key]
        if str(key) != f"f06:{manifest_ref}:enqueue":
            _require(
                raw_terminal_receipt == raw_baseline_receipt,
                "resume_checkpoint_receipt_changed",
                str(key),
            )
            continue
        _require(
            isinstance(raw_baseline_receipt, Mapping)
            and isinstance(raw_terminal_receipt, Mapping),
            "resume_enqueue_receipt_invalid",
        )
        assert isinstance(raw_baseline_receipt, Mapping)
        assert isinstance(raw_terminal_receipt, Mapping)
        if raw_terminal_receipt == raw_baseline_receipt:
            continue
        baseline_details = raw_baseline_receipt.get("details")
        terminal_details = raw_terminal_receipt.get("details")
        _require(
            isinstance(baseline_details, Mapping)
            and isinstance(terminal_details, Mapping),
            "resume_enqueue_receipt_details_invalid",
        )
        assert isinstance(baseline_details, Mapping)
        assert isinstance(terminal_details, Mapping)
        _require(
            str(raw_baseline_receipt.get("effect")) == "enqueue"
            and str(raw_terminal_receipt.get("effect")) == "enqueue"
            and bool(raw_terminal_receipt.get("ok"))
            and not bool(baseline_details.get("enqueue_admission_complete", False))
            and bool(terminal_details.get("enqueue_admission_complete", False))
            and int(terminal_details.get("queue_order_version", 0))
            >= int(baseline_details.get("queue_order_version", 0)),
            "resume_enqueue_receipt_transition_invalid",
        )
        if "baseline_dead_letter_ids" in baseline_details:
            _require(
                tuple(terminal_details.get("baseline_dead_letter_ids", ()))
                == tuple(baseline_details.get("baseline_dead_letter_ids", ())),
                "resume_enqueue_dlq_baseline_changed",
            )


def _recovery_capability_context(
    *,
    board_id: str,
    lifetime_probe: Callable[[], bool],
) -> Any:
    """Resolve the Core nominal issuer; no bool/string fallback is accepted."""

    from okto_pulse.core.kg.recovery_execution import (
        issue_recovery_execution_capability,
    )

    return issue_recovery_execution_capability(
        board_id=board_id,
        lifetime_probe=lifetime_probe,
    )


def _assert_legacy_queue_only_transition(
    intent: Any,
    *,
    before: QueueSnapshot,
    after: QueueSnapshot,
) -> None:
    from okto_pulse.community.adapters.legacy_rebuild_reconciliation import (
        legacy_queue_terminal_row,
    )

    _require(
        before.columns == after.columns,
        "legacy_queue_only_queue_columns_changed",
    )
    indexes = {column: index for index, column in enumerate(before.columns)}
    _require(
        {"id", "board_id", "source"} <= set(indexes),
        "legacy_queue_only_queue_columns_missing",
    )
    before_rows = {str(row[indexes["id"]]): row for row in before.rows}
    after_rows = {str(row[indexes["id"]]): row for row in after.rows}
    _require(
        len(before_rows) == len(before.rows)
        and len(after_rows) == len(after.rows)
        and set(before_rows) == set(after_rows),
        "legacy_queue_only_queue_identity_set_changed",
    )
    expected_rows = {str(row["id"]): dict(row) for row in intent.queue_rows}
    memberships = {str(row["row_id"]): dict(row) for row in intent.memberships}
    for row_id, before_row in before_rows.items():
        after_row = after_rows[row_id]
        expected = expected_rows.get(row_id)
        if expected is None:
            _require(
                after_row == before_row,
                "legacy_queue_only_unrelated_queue_row_changed",
                row_id,
            )
            continue
        terminal = legacy_queue_terminal_row(expected, memberships[row_id])
        before_mapping = dict(zip(before.columns, before_row, strict=True))
        after_mapping = dict(zip(after.columns, after_row, strict=True))
        _require(
            before_mapping in (expected, terminal) and after_mapping == terminal,
            "legacy_queue_only_target_transition_invalid",
            row_id,
        )


def _assert_legacy_queue_only_artifact_transition(
    reconciliation: LegacyQueueOnlyReconciliation,
    *,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
) -> None:
    intent = reconciliation.intent
    intent_payload = _legacy_intent_receipt_payload(intent)
    compensation_payload = _legacy_terminal_compensation_payload(intent)
    audit_payload = _legacy_nominal_audit_payload(intent)
    intent_relative = _legacy_effect_relative(str(intent_payload["effect_key"]))
    compensation_relative = _legacy_effect_relative(
        str(compensation_payload["effect_key"])
    )
    audit_relative = _legacy_effect_relative(str(audit_payload["effect_key"]))
    mutable = {
        reconciliation.checkpoint_relative,
        intent_relative,
        compensation_relative,
        audit_relative,
    }
    terminal = _snapshot_tree_hashes(rebuild_root)
    for relative in set(rebuild_baseline) | set(terminal):
        if relative not in mutable:
            _require(
                rebuild_baseline.get(relative) == terminal.get(relative),
                "legacy_queue_only_unrelated_rebuild_artifact_changed",
                relative,
            )
    _require(
        mutable <= set(terminal),
        "legacy_queue_only_terminal_artifact_missing",
        repr(sorted(mutable - set(terminal))),
    )
    _require(
        _read_baseline_json(
            rebuild_root,
            terminal,
            intent_relative,
            code="legacy_queue_only_terminal_intent",
        )
        == intent_payload
        and _read_baseline_json(
            rebuild_root,
            terminal,
            compensation_relative,
            code="legacy_queue_only_terminal_compensation",
        )
        == compensation_payload
        and _read_baseline_json(
            rebuild_root,
            terminal,
            audit_relative,
            code="legacy_queue_only_terminal_audit",
        )
        == audit_payload,
        "legacy_queue_only_terminal_artifact_payload_invalid",
    )
    checkpoint = _read_baseline_json(
        rebuild_root,
        terminal,
        reconciliation.checkpoint_relative,
        code="legacy_queue_only_terminal_checkpoint",
    )
    _assert_legacy_queue_only_terminal_checkpoint(intent, checkpoint)


async def _run_legacy_queue_only_lane(
    reconciliation: LegacyQueueOnlyReconciliation,
    *,
    bundle: ServiceBundle,
    composition: Any,
    db_path: Path,
    schema_fingerprint: str,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    board_storage_root: Path,
    board_storage_baseline: Mapping[str, str],
    actor_id: str,
    cancel_event: threading.Event,
    lifetime_probe: Callable[[], bool],
    timeout_seconds: float,
) -> dict[str, Any]:
    from okto_pulse.core.application.rebuild_processor import (
        validate_legacy_manual_restore_queue_only_outcome,
    )

    _require(not reconciliation.terminal, "legacy_queue_only_lane_already_terminal")
    logical_before = _sqlite_logical_fingerprints(
        db_path,
        exclude_tables=frozenset({"consolidation_queue"}),
    )
    queue_before = _full_queue_snapshot(db_path)
    dlq_before = _dlq_snapshot(db_path)
    service_task: asyncio.Task[Any] | None = None
    with _recovery_capability_context(
        board_id=reconciliation.intent.board_id,
        lifetime_probe=lifetime_probe,
    ) as capability:
        _require(lifetime_probe(), "legacy_queue_only_lifetime_lost_before_service")
        service_task = asyncio.create_task(
            asyncio.to_thread(
                bundle.service.legacy_manual_restore_queue_only,
                board_id=reconciliation.intent.board_id,
                intent_id=reconciliation.intent.evidence_digest,
                actor_id=actor_id,
                reason=str(reconciliation.intent.payload["recovery_reason"]),
                command=reconciliation.command,
                intent_receipt=reconciliation.intent_receipt,
                recovery_capability=capability,
            ),
            name="offline-legacy-manual-restore-queue-only",
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(service_task),
                timeout=timeout_seconds,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError, KeyboardInterrupt):
            await _await_service_fenced(service_task, cancel_event=cancel_event)
            service_task = None
            raise
        service_task = None
        validate_legacy_manual_restore_queue_only_outcome(
            result,
            command=reconciliation.command,
            intent_receipt=reconciliation.intent_receipt,
        )
        _require(
            lifetime_probe(),
            "legacy_queue_only_lifetime_lost_before_terminal_gate",
        )
        _assert_schema_unchanged(db_path, schema_fingerprint)
        queue_after = _full_queue_snapshot(db_path)
        _assert_legacy_queue_only_transition(
            reconciliation.intent,
            before=queue_before,
            after=queue_after,
        )
        _assert_unchanged_snapshot(
            _dlq_snapshot(db_path),
            dlq_before,
            "legacy_queue_only_dlq_changed",
        )
        _require(
            _sqlite_logical_fingerprints(
                db_path,
                exclude_tables=frozenset({"consolidation_queue"}),
            )
            == logical_before,
            "legacy_queue_only_unrelated_database_changed",
        )
        _require(
            _snapshot_tree_hashes(quarantine_root) == dict(quarantine_baseline),
            "legacy_queue_only_quarantine_changed",
        )
        closed_board = _snapshot_closed_board_storage(
            board_id=reconciliation.intent.board_id,
            board_storage_root=board_storage_root,
            phase="legacy_manual_restore_queue_only_terminal",
        )
        _require(
            closed_board == dict(board_storage_baseline),
            "legacy_queue_only_board_storage_changed",
        )
        _assert_legacy_queue_only_artifact_transition(
            reconciliation,
            rebuild_root=rebuild_root,
            rebuild_baseline=rebuild_baseline,
        )
        terminal_rows, adoption = _legacy_queue_state_current(
            reconciliation.intent,
            db_path=db_path,
        )
        _require(terminal_rows, "legacy_queue_only_rows_not_terminal")
        _require(
            bundle.single_writer_lock.inspect(board_id=reconciliation.intent.board_id)
            is None
            and bundle.operation_reservation.inspect(
                board_id=reconciliation.intent.board_id
            )
            is None,
            "legacy_queue_only_core_fence_not_released",
        )
        _assert_worker_registry_never_started(composition)
        return {
            "_recovery_phase": "reconciled",
            "reconciliation_kind": "legacy_manual_restore_queue_only",
            "reconciled_run_id": reconciliation.intent.f06_run_id,
            "legacy_intent_digest": reconciliation.intent.evidence_digest,
            "adopted_identity_count": len(adoption.identities),
        }


async def _wait_for_admission_or_post_drain(
    service_task: asyncio.Task[Any],
    bundle: ServiceBundle,
    *,
    board_id: str,
    manifest_ref: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[Mapping[str, Any], Any | None, bool]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    source = f"rebuild:{manifest_ref}"
    while True:
        checkpoint = _load_checkpoint(bundle, board_id, manifest_ref)
        if isinstance(checkpoint, Mapping):
            state = _checkpoint_state(checkpoint)
            if state == CHECKPOINT_STATE_DRAINING:
                if (
                    _active_exact_queue_depth(
                        Path(os.environ["DATA_DIR"]) / "data" / "pulse.db",
                        board_id=board_id,
                        source=source,
                    )
                    > 0
                ):
                    return checkpoint, None, True
            elif state in POST_DRAIN_CHECKPOINT_STATES:
                return checkpoint, None, False
        if service_task.done():
            result = await service_task
            checkpoint = _load_checkpoint(bundle, board_id, manifest_ref)
            if (
                isinstance(checkpoint, Mapping)
                and _checkpoint_state(checkpoint) in POST_DRAIN_CHECKPOINT_STATES
            ):
                return checkpoint, result, False
            raise RecoveryRefused(
                "rebuild_terminated_before_drain_admission:"
                f"outcome={getattr(result, 'outcome', None)} "
                f"reason={getattr(result, 'reason', None)}"
            )
        _require(
            asyncio.get_running_loop().time() < deadline,
            "rebuild_admission_timeout",
        )
        await asyncio.sleep(poll_seconds)


async def _await_service_fenced(
    service_task: asyncio.Task[Any],
    *,
    cancel_event: threading.Event,
) -> None:
    """Wait without a teardown deadline while the native rebuild may mutate.

    Cancellation requests stop admission/progress, but they must never revoke
    the recovery capability, close graphs/SQLite, or release the serve lock
    while ``KGRebuildService.run`` is still executing in its OS thread.
    """

    cancel_event.set()
    watchdog_seconds = 30.0
    while not service_task.done():
        try:
            done, _pending = await asyncio.shield(
                asyncio.wait({service_task}, timeout=watchdog_seconds)
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            _emit("rebuild_cancellation_deferred_until_native_exit")
            continue
        if not done:
            _emit(
                "rebuild_native_exit_watchdog",
                fenced=True,
                teardown_deferred=True,
            )
    try:
        service_task.result()
    except BaseException as exc:
        _emit(
            "rebuild_service_terminal_after_cancel",
            error_type=type(exc).__name__,
            error=str(exc),
        )


async def _drain_exact_scope(
    service_task: asyncio.Task[Any],
    processor: Any,
    claim_scope: Any,
    bundle: ServiceBundle,
    manifest: Any,
    *,
    board_id: str,
    source: str,
    baseline_dlq: QueueSnapshot,
    baseline_non_target: QueueSnapshot,
    admitted_identities: set[tuple[str, str]],
    cancel_event: threading.Event,
    lifetime_probe: Callable[[], bool],
    timeout_seconds: float,
    poll_seconds: float,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    db_path = Path(os.environ["DATA_DIR"]) / "data" / "pulse.db"
    batches = 0
    processed_total = 0
    cancellation_wait_emitted = False
    while not service_task.done():
        _require(lifetime_probe(), "recovery_capability_lifetime_lost")
        if cancel_event.is_set():
            if not cancellation_wait_emitted:
                _emit(
                    "exact_scope_claims_stopped_for_cancellation",
                    fenced=True,
                )
                cancellation_wait_emitted = True
            _assert_source_unchanged(bundle, manifest, board_id)
            _assert_unchanged_snapshot(
                _dlq_snapshot(db_path),
                baseline_dlq,
                "dead_letter_set_changed_during_cancellation",
            )
            _assert_unchanged_snapshot(
                _protected_queue_snapshot(
                    db_path,
                    board_id=board_id,
                    admitted_identities=admitted_identities,
                ),
                baseline_non_target,
                "non_target_queue_changed_during_cancellation",
            )
            await asyncio.sleep(poll_seconds)
            continue
        _require(
            asyncio.get_running_loop().time() < deadline,
            "rebuild_run_timeout",
        )
        _assert_reservation_exact(
            bundle,
            board_id=board_id,
            manifest_ref=manifest.manifest_ref,
        )
        _assert_source_unchanged(bundle, manifest, board_id)
        _assert_unchanged_snapshot(
            _dlq_snapshot(Path(os.environ["DATA_DIR"]) / "data" / "pulse.db"),
            baseline_dlq,
            "dead_letter_set_changed",
        )
        _assert_unchanged_snapshot(
            _protected_queue_snapshot(
                db_path,
                board_id=board_id,
                admitted_identities=admitted_identities,
            ),
            baseline_non_target,
            "non_target_queue_changed",
        )
        processed = int(await processor.process_batch(claim_scope=claim_scope))
        batches += 1
        processed_total += processed
        if processed == 0 and not service_task.done():
            attempted = int(getattr(processor, "last_attempted_count", 0))
            depth = _active_exact_queue_depth(
                db_path,
                board_id=board_id,
                source=source,
            )
            _require(
                attempted == 0 and depth == 0,
                "exact_processor_made_no_progress",
                f"attempted={attempted} depth={depth}",
            )
            await asyncio.sleep(poll_seconds)
    result = await service_task
    _emit(
        "exact_scope_drained",
        batches=batches,
        processed_total=processed_total,
        service_outcome=getattr(result, "outcome", None),
    )
    return result


async def _await_terminal_without_claims(
    service_task: asyncio.Task[Any],
    bundle: ServiceBundle,
    manifest: Any,
    *,
    board_id: str,
    baseline_dlq: QueueSnapshot,
    baseline_non_target: QueueSnapshot,
    admitted_identities: set[tuple[str, str]],
    cancel_event: threading.Event,
    lifetime_probe: Callable[[], bool],
    timeout_seconds: float,
    poll_seconds: float,
) -> Any:
    """Monitor a post-drain resume without ever starting queue consumers."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    db_path = Path(os.environ["DATA_DIR"]) / "data" / "pulse.db"
    while not service_task.done():
        _require(lifetime_probe(), "recovery_capability_lifetime_lost")
        _assert_source_unchanged(bundle, manifest, board_id)
        _assert_unchanged_snapshot(
            _dlq_snapshot(db_path),
            baseline_dlq,
            "dead_letter_set_changed_during_post_drain_resume",
        )
        _assert_unchanged_snapshot(
            _protected_queue_snapshot(
                db_path,
                board_id=board_id,
                admitted_identities=admitted_identities,
            ),
            baseline_non_target,
            "non_target_queue_changed_during_post_drain_resume",
        )
        if cancel_event.is_set():
            await asyncio.sleep(poll_seconds)
            continue
        _require(
            asyncio.get_running_loop().time() < deadline,
            "rebuild_run_timeout",
        )
        await asyncio.sleep(poll_seconds)
    return await service_task


async def _await_manifest_compensation(
    service_task: asyncio.Task[Any],
    bundle: ServiceBundle,
    manifest: AuthorizedManifestBinding,
    *,
    board_id: str,
    db_path: Path,
    baseline_dlq: QueueSnapshot,
    checkpoint_baseline: Mapping[str, Any] | None,
    cancel_event: threading.Event,
    lifetime_probe: Callable[[], bool],
    timeout_seconds: float,
    poll_seconds: float,
) -> Any:
    """Await Core ``fail_existing`` without turning drift into rebuild authority."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not service_task.done():
        _require(lifetime_probe(), "recovery_capability_lifetime_lost")
        _assert_unchanged_snapshot(
            _dlq_snapshot(db_path),
            baseline_dlq,
            "dead_letter_set_changed_during_manifest_compensation",
        )
        if cancel_event.is_set():
            await asyncio.sleep(poll_seconds)
            continue
        _require(
            asyncio.get_running_loop().time() < deadline,
            "manifest_compensation_timeout",
        )
        await asyncio.sleep(poll_seconds)

    result = await service_task
    _require(
        getattr(result, "outcome", None) == "manifest_drift"
        and getattr(result, "reason", None) == "manifest_drift",
        "manifest_compensation_outcome_invalid",
        f"outcome={getattr(result, 'outcome', None)} reason={getattr(result, 'reason', None)}",
    )
    _require(
        bool(getattr(result, "audit_ref", None)), "manifest_compensation_audit_missing"
    )
    audit = bundle.artifact_store.read_json_reference(str(result.audit_ref))
    _require(
        isinstance(audit, Mapping)
        and str(audit.get("run_id")) == str(result.run_id)
        and str(audit.get("board_id")) == board_id
        and str(audit.get("manifest_ref")) == manifest.manifest_ref
        and str(audit.get("outcome")) == "manifest_drift",
        "manifest_compensation_audit_invalid",
    )
    checkpoint = _load_checkpoint(bundle, board_id, manifest.manifest_ref)
    if checkpoint_baseline is not None:
        _require(
            isinstance(checkpoint, Mapping)
            and _checkpoint_state(checkpoint) == "failed",
            "manifest_compensation_checkpoint_not_failed",
        )
        assert isinstance(checkpoint, Mapping)
        checkpoint_command = dict(checkpoint.get("command") or {})
        baseline_command = dict(checkpoint_baseline.get("command") or {})
        _require(
            all(
                checkpoint_command.get(field) == baseline_command.get(field)
                for field in (
                    "run_id",
                    "board_id",
                    "manifest_ref",
                    "operation",
                    "actor_id",
                    "reason",
                    "source_rows",
                    "previous_generation_id",
                    "candidate_generation_id",
                )
            ),
            "manifest_compensation_checkpoint_command_changed",
        )
    _assert_unchanged_snapshot(
        _dlq_snapshot(db_path),
        baseline_dlq,
        "dead_letter_set_changed_after_manifest_compensation",
    )
    _require(
        _active_exact_queue_depth(
            db_path,
            board_id=board_id,
            source=f"rebuild:{manifest.manifest_ref}",
        )
        == 0,
        "manifest_compensation_exact_queue_not_empty",
    )
    _require(
        bundle.single_writer_lock.inspect(board_id=board_id) is None,
        "manifest_compensation_writer_lock_present",
    )
    _require(
        bundle.operation_reservation.inspect(board_id=board_id) is None,
        "manifest_compensation_reservation_present",
    )
    return result


async def _assert_manifest_compensation_reconciliation(
    bundle: ServiceBundle,
    plan: RecoveryRunPlan,
    result: Any,
    composition: Any,
    *,
    board_id: str,
    db_path: Path,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    quarantine_baseline_ids: frozenset[str],
    board_storage_root: Path,
    board_storage_baseline: Mapping[str, str],
    expected_board_storage: Mapping[str, str],
    baseline_queue: QueueSnapshot,
    baseline_dlq: QueueSnapshot,
    logical_database_baseline: Mapping[str, str],
    baseline_health: Mapping[str, Any],
    checkpoint_baseline: Mapping[str, Any] | None,
) -> None:
    """Prove a compensation lane before any fresh rebaseline is permitted."""

    from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects

    manifest = plan.manifest
    receipt = plan.receipt
    _require(
        plan.mode == "resume_compensation"
        and isinstance(manifest, AuthorizedManifestBinding)
        and isinstance(receipt, Mapping),
        "manifest_compensation_plan_invalid",
    )
    assert isinstance(manifest, AuthorizedManifestBinding)
    assert isinstance(receipt, Mapping)
    run_id = str(receipt.get("run_id") or "")
    f06_run_id = f"f06:{manifest.manifest_ref}"
    checkpoint_id = CommunityRebuildEffects._checkpoint_id(f06_run_id)
    compensate_key = f"{f06_run_id}:compensate"
    compensate_id = CommunityRebuildEffects._effect_id(compensate_key)
    audit_key = f"{f06_run_id}:audit:manifest_drift"
    audit_effect_id = CommunityRebuildEffects._effect_id(audit_key)
    active_relative = f"audit/confirmation_receipts/{board_id}/active.json"
    history_relative = f"audit/confirmation_receipts/{board_id}/{run_id}.json"
    allowed_mutations = {
        active_relative,
        history_relative,
        f"audit/{run_id}.json",
        f"audit/{checkpoint_id}.json",
        f"audit/{compensate_id}.json",
        f"audit/{audit_effect_id}.json",
    }
    after = _snapshot_tree_hashes(rebuild_root)
    changed_unrelated = sorted(
        relative
        for relative in set(after) | set(rebuild_baseline)
        if after.get(relative) != rebuild_baseline.get(relative)
        and relative not in allowed_mutations
    )
    _require(
        not changed_unrelated,
        "manifest_compensation_unrelated_rebuild_artifact_changed",
        repr(changed_unrelated),
    )
    expected_terminal_receipt = {**dict(receipt), "receipt_state": "terminal"}
    _require(
        _load_json_file(
            rebuild_root / active_relative,
            "manifest_compensation_active_receipt_missing",
        )
        == expected_terminal_receipt
        and _load_json_file(
            rebuild_root / history_relative,
            "manifest_compensation_history_receipt_missing",
        )
        == expected_terminal_receipt,
        "manifest_compensation_receipt_transition_invalid",
        run_id,
    )
    checkpoint = _load_checkpoint(bundle, board_id, manifest.manifest_ref)
    _require(
        isinstance(checkpoint, Mapping) and _checkpoint_state(checkpoint) == "failed",
        "manifest_compensation_checkpoint_invalid",
    )
    assert isinstance(checkpoint, Mapping)
    receipts = checkpoint.get("receipts")
    compensate_receipt = (
        receipts.get(compensate_key) if isinstance(receipts, Mapping) else None
    )
    compensation_applied = isinstance(compensate_receipt, Mapping)
    if compensation_applied:
        assert isinstance(compensate_receipt, Mapping)
        _require(
            compensate_receipt.get("ok") is True
            and str(compensate_receipt.get("effect")) == "compensate"
            and str(compensate_receipt.get("code")) == "compensated",
            "manifest_compensation_effect_receipt_invalid",
        )
        _require(
            _load_json_file(
                rebuild_root / f"audit/{compensate_id}.json",
                "manifest_compensation_effect_artifact_missing",
            )
            == compensate_receipt,
            "manifest_compensation_effect_artifact_mismatch",
        )
    else:
        _require(
            isinstance(checkpoint_baseline, Mapping)
            and _checkpoint_state(checkpoint_baseline) in {"planned", "snapshotted"}
            and not tuple(checkpoint.get("compensation_actions") or ())
            and f"audit/{compensate_id}.json" not in after,
            "manifest_compensation_noop_state_invalid",
        )
    audit_effect_path = rebuild_root / f"audit/{audit_effect_id}.json"
    if audit_effect_path.exists():
        audit_effect = _load_json_file(
            audit_effect_path,
            "manifest_compensation_audit_effect_invalid",
        )
        _require(
            audit_effect.get("ok") is True
            and str(audit_effect.get("effect")) == "audit"
            and str(audit_effect.get("effect_key")) == audit_key,
            "manifest_compensation_audit_effect_mismatch",
        )
    _assert_compensation_queue_transition(
        baseline_queue,
        _full_queue_snapshot(db_path),
        board_id=board_id,
        source=f"rebuild:{manifest.manifest_ref}",
    )
    _assert_unchanged_snapshot(
        _dlq_snapshot(db_path),
        baseline_dlq,
        "dead_letter_set_changed_after_manifest_compensation",
    )
    _require(
        _sqlite_logical_fingerprints(
            db_path,
            exclude_tables=frozenset({"consolidation_queue"}),
        )
        == dict(logical_database_baseline),
        "logical_database_changed_during_manifest_compensation",
    )
    _assert_tree_preserved(quarantine_root, quarantine_baseline)
    after_quarantine_ids = _quarantine_ids(quarantine_root)
    new_quarantine_ids = after_quarantine_ids - quarantine_baseline_ids
    details = compensate_receipt.get("details") if compensation_applied else {}
    _require(isinstance(details, Mapping), "manifest_compensation_details_invalid")
    assert isinstance(details, Mapping)
    restore = details.get("quarantine_restore")
    candidate_discard = details.get("candidate_discard")
    expected_backup_id: str | None = None
    if isinstance(restore, Mapping):
        _require(
            restore.get("ok") is True,
            "manifest_compensation_quarantine_restore_invalid",
        )
        report = restore.get("report")
        if isinstance(report, Mapping) and report.get("backup_quarantine_id"):
            _require(
                report.get("applied") is True
                and report.get("open_validated") is True
                and str(report.get("board_id")) == board_id,
                "manifest_compensation_restore_report_invalid",
            )
            expected_backup_id = str(report["backup_quarantine_id"])
    if isinstance(candidate_discard, Mapping) and candidate_discard.get(
        "candidate_quarantine_id"
    ):
        candidate_backup_id = str(candidate_discard["candidate_quarantine_id"])
        _require(
            expected_backup_id in (None, candidate_backup_id),
            "manifest_compensation_backup_quarantine_conflict",
        )
        expected_backup_id = candidate_backup_id
    _require(
        new_quarantine_ids
        == ({expected_backup_id} if expected_backup_id is not None else set()),
        "manifest_compensation_quarantine_set_invalid",
        repr(sorted(new_quarantine_ids)),
    )
    expected_terminal_board_storage = (
        dict(expected_board_storage)
        if compensation_applied
        else dict(board_storage_baseline)
    )
    terminal_board_storage = _snapshot_closed_board_storage(
        board_id=board_id,
        board_storage_root=board_storage_root,
        phase="manifest_compensation_terminal",
    )
    _require(
        terminal_board_storage == expected_terminal_board_storage,
        "manifest_compensation_board_storage_mismatch",
    )
    post_health = _offline_cold_graph_health(
        bundle,
        board_id=board_id,
        board_storage_root=board_storage_root,
        board_storage_snapshot=terminal_board_storage,
    )
    _require(
        post_health.get("current_kg_generation_id")
        == baseline_health.get("current_kg_generation_id"),
        "manifest_compensation_generation_pointer_changed",
    )
    graph_files = terminal_board_storage
    if graph_files:
        _require(
            bool(post_health.get("graph_storage_exists")),
            "manifest_compensation_graph_storage_unresolved",
        )
    else:
        _require(
            post_health.get("current_kg_generation_id") is None,
            "manifest_compensation_missing_graph_has_generation",
        )
    _require(
        bundle.single_writer_lock.inspect(board_id=board_id) is None,
        "manifest_compensation_writer_lock_present",
    )
    _require(
        bundle.operation_reservation.inspect(board_id=board_id) is None,
        "manifest_compensation_reservation_present",
    )


async def _await_closed_archive_reconciliation(
    service_task: asyncio.Task[Any],
    bundle: ServiceBundle,
    plan: RecoveryRunPlan,
    composition: Any,
    *,
    board_id: str,
    db_path: Path,
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    board_storage_root: Path,
    board_storage_baseline: Mapping[str, str],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    baseline_queue: QueueSnapshot,
    baseline_dlq: QueueSnapshot,
    logical_database_baseline: Mapping[str, str],
    baseline_health: Mapping[str, Any],
    cancel_event: threading.Event,
    lifetime_probe: Callable[[], bool],
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    """Archive one already-closed failed receipt and prove zero other effects."""

    from okto_pulse.core.kg.rebuild_service import (
        is_rebuild_terminal_audit_closed,
        is_rebuild_terminal_audit_frozen,
    )
    from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey

    receipt = plan.receipt
    audit = plan.terminal_audit
    _require(
        plan.mode == "archive_closed"
        and isinstance(receipt, Mapping)
        and isinstance(audit, Mapping),
        "archive_closed_plan_invalid",
    )
    assert isinstance(receipt, Mapping)
    assert isinstance(audit, Mapping)
    run_id = str(receipt.get("run_id") or "")
    _require(
        bool(run_id)
        and is_rebuild_terminal_audit_closed(audit)
        and not is_rebuild_terminal_audit_frozen(audit)
        and str(audit.get("outcome") or "") != "completed"
        and not any(
            (
                audit.get("report_ref"),
                audit.get("report_id"),
                audit.get("current_kg_generation_id"),
                audit.get("event_emitted") is True,
                audit.get("promotion_outcome") == "promoted",
                audit.get("publishable_status") == "completed",
            )
        ),
        "archive_closed_audit_not_receipt_only",
        run_id,
    )
    _require(
        receipt.get("receipt_state") in (None, "authorized"),
        "archive_closed_receipt_state_invalid",
        run_id,
    )
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not service_task.done():
        _require(lifetime_probe(), "recovery_capability_lifetime_lost")
        _assert_unchanged_snapshot(
            _full_queue_snapshot(db_path),
            baseline_queue,
            "queue_changed_during_closed_receipt_archive",
        )
        _assert_unchanged_snapshot(
            _dlq_snapshot(db_path),
            baseline_dlq,
            "dead_letter_set_changed_during_closed_receipt_archive",
        )
        if cancel_event.is_set():
            await asyncio.sleep(poll_seconds)
            continue
        _require(
            asyncio.get_running_loop().time() < deadline,
            "closed_receipt_archive_timeout",
        )
        await asyncio.sleep(poll_seconds)

    result = await service_task
    _require(
        all(
            (
                str(getattr(result, "run_id", "")) == run_id,
                str(getattr(result, "outcome", "")) == str(audit.get("outcome")),
                str(getattr(result, "reason", "")) == str(audit.get("reason")),
                str(getattr(result, "audit_ref", ""))
                == str(
                    bundle.artifact_store.reference(
                        RebuildAuditKey(
                            namespace="run_audit",
                            board_id=board_id,
                            artifact_id=run_id,
                        )
                    )
                ),
                getattr(result, "report_ref", None) is None,
                getattr(result, "report_id", None) is None,
                getattr(result, "current_kg_generation_id", None) is None,
                getattr(result, "event_emitted", False) is False,
            )
        ),
        "archive_closed_result_mismatch",
        run_id,
    )
    _require(
        bundle.artifact_store.read_json_reference(str(result.audit_ref)) == dict(audit),
        "archive_closed_run_audit_changed",
        run_id,
    )

    active_relative = f"audit/confirmation_receipts/{board_id}/active.json"
    history_relative = f"audit/confirmation_receipts/{board_id}/{run_id}.json"
    after = _snapshot_tree_hashes(rebuild_root)
    changed_unrelated = sorted(
        relative
        for relative in set(after) | set(rebuild_baseline)
        if after.get(relative) != rebuild_baseline.get(relative)
        and relative not in {active_relative, history_relative}
    )
    _require(
        not changed_unrelated,
        "archive_closed_unrelated_rebuild_artifact_changed",
        repr(changed_unrelated),
    )
    expected_terminal_receipt = {**dict(receipt), "receipt_state": "terminal"}
    _require(
        _load_json_file(
            rebuild_root / active_relative,
            "archive_closed_active_receipt_missing",
        )
        == expected_terminal_receipt
        and _load_json_file(
            rebuild_root / history_relative,
            "archive_closed_history_receipt_missing",
        )
        == expected_terminal_receipt,
        "archive_closed_receipt_transition_invalid",
        run_id,
    )
    _assert_unchanged_snapshot(
        _full_queue_snapshot(db_path),
        baseline_queue,
        "queue_changed_during_closed_receipt_archive",
    )
    _assert_unchanged_snapshot(
        _dlq_snapshot(db_path),
        baseline_dlq,
        "dead_letter_set_changed_during_closed_receipt_archive",
    )
    _require(
        _sqlite_logical_fingerprints(db_path) == dict(logical_database_baseline),
        "logical_database_changed_during_closed_receipt_archive",
    )
    terminal_board_storage = _snapshot_closed_board_storage(
        board_id=board_id,
        board_storage_root=board_storage_root,
        phase="archive_closed_terminal",
    )
    _require(
        terminal_board_storage == dict(board_storage_baseline),
        "board_storage_changed_during_closed_receipt_archive",
    )
    _require(
        _snapshot_tree_hashes(quarantine_root) == dict(quarantine_baseline),
        "quarantine_changed_during_closed_receipt_archive",
    )
    post_health = _offline_cold_graph_health(
        bundle,
        board_id=board_id,
        board_storage_root=board_storage_root,
        board_storage_snapshot=terminal_board_storage,
    )
    _require(
        all(
            post_health.get(field) == baseline_health.get(field)
            for field in ("graph_state", "current_kg_generation_id")
        ),
        "graph_health_changed_during_closed_receipt_archive",
    )
    _require(
        bundle.single_writer_lock.inspect(board_id=board_id) is None,
        "archive_closed_writer_lock_present",
    )
    _require(
        bundle.operation_reservation.inspect(board_id=board_id) is None,
        "archive_closed_reservation_present",
    )
    return {
        "_recovery_phase": "reconciled",
        "reconciliation_kind": "archive_closed",
        "reconciled_run_id": run_id,
    }


async def _assert_terminal_gates(
    composition: Any,
    bundle: ServiceBundle,
    manifest: Any,
    result: Any,
    *,
    board_id: str,
    actor_id: str,
    db_path: Path,
    source: str,
    baseline_dlq: QueueSnapshot,
    baseline_non_target: QueueSnapshot,
    admitted_identities: set[tuple[str, str]],
    expected_source_rows: Sequence[Mapping[str, Any]],
    expected_cognitive_source_rows: Sequence[Mapping[str, Any]],
    quarantine_root: Path,
    quarantine_baseline: Mapping[str, str],
    quarantine_baseline_ids: frozenset[str],
    rebuild_root: Path,
    rebuild_baseline: Mapping[str, str],
    preflight_hash: str,
    confirmation_ref: str,
    user_reason: str,
    manifest_artifact_hash: str,
    resume_mode: bool,
    resume_checkpoint_baseline: Mapping[str, Any] | None,
    overlay_revision_baseline: Mapping[str, Any] | None,
    expect_overlay_revision_advance: bool,
    cognitive_ledger_baseline: CognitiveLedgerBaseline,
    frozen_confirmation_receipt_baseline: Mapping[str, Any] | None,
    rebaseline_audit_baseline: Mapping[str, Any] | None = None,
    expected_rebaseline_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from okto_pulse.core.application.kg_runtime_access import resolve_graph_lifecycle
    from okto_pulse.core.kg.rebuild_report import RebuildReportStore

    _require(getattr(result, "outcome", None) == "completed", "rebuild_not_completed")
    _require(
        getattr(result, "publishable_status", None) == "completed",
        "rebuild_not_publishable_completed",
    )
    audit_ref = str(getattr(result, "audit_ref", None) or "")
    _require(bool(audit_ref), "terminal_run_audit_ref_missing")
    audit = bundle.artifact_store.read_json_reference(audit_ref)
    _require(isinstance(audit, Mapping), "terminal_run_audit_unreadable")
    assert isinstance(audit, Mapping)
    _require(
        str(audit.get("run_id")) == str(result.run_id), "terminal_run_audit_id_mismatch"
    )
    _require(
        str(audit.get("board_id")) == board_id, "terminal_run_audit_board_mismatch"
    )
    _require(
        str(audit.get("outcome")) == "completed", "terminal_run_audit_outcome_invalid"
    )
    _require(
        str(audit.get("manifest_ref")) == str(manifest.manifest_ref),
        "terminal_run_audit_manifest_mismatch",
    )
    _require(
        str(audit.get("confirmation_ref")) == confirmation_ref,
        "terminal_run_audit_confirmation_mismatch",
    )
    _require(
        str(audit.get("user_reason")) == user_reason,
        "terminal_run_audit_reason_mismatch",
    )
    _require(
        bool(getattr(result, "event_emitted", False)), "kg_rebuilt_event_not_emitted"
    )
    _require(
        getattr(result, "promotion_outcome", None) == "promoted",
        "generation_not_promoted",
        str(getattr(result, "promotion_outcome", None)),
    )
    current_generation = getattr(result, "current_kg_generation_id", None)
    _require(bool(current_generation), "current_generation_missing")
    report_ref = getattr(result, "report_ref", None)
    report_id = getattr(result, "report_id", None)
    _require(bool(report_ref) and bool(report_id), "terminal_report_missing")
    _require(
        str(audit.get("report_ref")) == str(report_ref)
        and str(audit.get("report_id")) == str(report_id)
        and str(audit.get("publishable_status")) == "completed"
        and str(audit.get("promotion_outcome")) == "promoted"
        and str(audit.get("current_kg_generation_id")) == str(current_generation)
        and bool(audit.get("event_emitted")),
        "terminal_run_audit_receipts_mismatch",
    )
    report = RebuildReportStore(artifact_store=bundle.artifact_store).load(report_ref)
    _require(isinstance(report, Mapping), "terminal_report_unreadable")
    assert isinstance(report, Mapping)
    _require(
        str(report.get("report_id")) == str(report_id), "terminal_report_id_mismatch"
    )
    summary = report.get("summary")
    _require(isinstance(summary, Mapping), "terminal_report_summary_missing")
    assert isinstance(summary, Mapping)
    _require(
        str(summary.get("board_id")) == board_id,
        "terminal_report_board_mismatch",
    )
    _require(
        str(summary.get("run_id")) == str(result.run_id), "terminal_report_run_mismatch"
    )
    _require(
        str(summary.get("status")) == "completed", "terminal_report_status_invalid"
    )
    _require(
        str(summary.get("kg_generation_id")) == str(current_generation),
        "terminal_report_generation_mismatch",
    )
    _require(
        tuple(str(value) for value in report.get("source_refs", ()))
        == (manifest.manifest_ref,),
        "terminal_report_manifest_ref_mismatch",
    )
    hashes = report.get("hashes")
    _require(isinstance(hashes, Mapping), "terminal_report_hashes_missing")
    assert isinstance(hashes, Mapping)
    from okto_pulse.core.kg.rebuild_deterministic import compute_source_hash

    expected_source_hash = compute_source_hash(expected_source_rows)
    _require(
        str(hashes.get("source_hash")) == expected_source_hash,
        "terminal_report_source_hash_mismatch",
    )
    terminal_source_set = bundle.source_enumerator.enumerate(board_id=board_id)
    try:
        persisted_manifest = bundle.manifest_store.load_verified(
            manifest.manifest_ref,
            expected_board_id=board_id,
            expected_preflight_hash=preflight_hash,
            cognitive_digest=terminal_source_set.cognitive_durable_digest,
        )
    except Exception as exc:
        raise RecoveryRefused("terminal_manifest_verification_failed") from exc
    _require(
        str(persisted_manifest.board_id) == board_id
        and str(persisted_manifest.source_set_hash) == str(manifest.source_set_hash),
        "terminal_manifest_hash_or_board_mismatch",
    )
    _manifest_reference, terminal_manifest_hash = _manifest_artifact_fingerprint(
        bundle,
        manifest_ref=manifest.manifest_ref,
    )
    _require(
        terminal_manifest_hash == manifest_artifact_hash,
        "source_manifest_changed_during_recovery",
    )
    _require(
        bundle.generation_repository.get_current(board_id) == current_generation,
        "current_generation_pointer_mismatch",
    )

    checkpoint = _load_checkpoint(bundle, board_id, manifest.manifest_ref)
    _require(isinstance(checkpoint, Mapping), "terminal_checkpoint_missing")
    assert isinstance(checkpoint, Mapping)
    _assert_resume_checkpoint_monotonic(
        resume_checkpoint_baseline,
        checkpoint,
        manifest_ref=manifest.manifest_ref,
    )
    _require(
        _checkpoint_state(checkpoint) == CHECKPOINT_STATE_COMPLETED,
        "terminal_checkpoint_not_completed",
        _checkpoint_state(checkpoint),
    )
    _require(
        int(checkpoint.get("writer_handoff_count", 0)) == 1,
        "terminal_writer_handoff_invalid",
    )
    _require(
        int(checkpoint.get("writer_reacquire_count", 0)) == 1,
        "terminal_writer_reacquire_invalid",
    )
    receipts = checkpoint.get("receipts")
    _require(isinstance(receipts, Mapping), "terminal_checkpoint_receipts_missing")
    assert isinstance(receipts, Mapping)
    expected_receipt_keys = {
        f"f06:{manifest.manifest_ref}:{effect}"
        for effect in ("snapshot", "quarantine", "enqueue", "restore", "promote")
    }
    _require(
        set(str(key) for key in receipts) == expected_receipt_keys,
        "terminal_checkpoint_receipt_set_invalid",
        repr(sorted(str(key) for key in receipts)),
    )
    receipt_by_effect: dict[str, Mapping[str, Any]] = {}
    for effect in ("snapshot", "quarantine", "enqueue", "restore", "promote"):
        key = f"f06:{manifest.manifest_ref}:{effect}"
        receipt = receipts.get(key)
        _require(
            isinstance(receipt, Mapping), "terminal_checkpoint_receipt_invalid", key
        )
        assert isinstance(receipt, Mapping)
        _require(bool(receipt.get("ok")), "terminal_checkpoint_receipt_failed", key)
        _require(
            str(receipt.get("effect")) == effect,
            "terminal_checkpoint_effect_mismatch",
            key,
        )
        receipt_by_effect[effect] = receipt
    _require(
        _active_exact_queue_depth(db_path, board_id=board_id, source=source) == 0,
        "terminal_exact_queue_not_empty",
    )
    _require(
        not _exact_queue_rows(
            db_path,
            board_id=board_id,
            source=source,
        ).rows,
        "terminal_exact_queue_rows_remain",
    )
    _require(
        _active_rebuild_queue_depth(db_path, board_id=board_id) == 0,
        "terminal_board_rebuild_queue_not_empty",
    )
    _assert_unchanged_snapshot(
        _dlq_snapshot(db_path),
        baseline_dlq,
        "terminal_dead_letter_set_changed",
    )
    _assert_unchanged_snapshot(
        _protected_queue_snapshot(
            db_path,
            board_id=board_id,
            admitted_identities=admitted_identities,
        ),
        baseline_non_target,
        "terminal_non_target_queue_changed",
    )
    if expect_overlay_revision_advance:
        _assert_source_unchanged(
            bundle,
            manifest,
            board_id,
            require_terminal_binding=True,
            expected_rebaseline_target_source_set_hash=(
                str(expected_rebaseline_evidence.get("to_source_set_hash") or "")
                if expected_rebaseline_evidence is not None
                else None
            ),
        )
    _require(
        bundle.single_writer_lock.inspect(board_id=board_id) is None,
        "terminal_writer_lock_present",
    )
    _require(
        bundle.operation_reservation.inspect(board_id=board_id) is None,
        "terminal_administrative_reservation_present",
    )

    graph_handle = await resolve_graph_lifecycle().open(board_id)
    _require(bool(graph_handle.opened), "terminal_graph_not_opened")
    _require(not bool(graph_handle.quarantined), "terminal_graph_quarantined")
    orphan_report = bundle.orphan_scanner.scan(
        board_id=board_id,
        generation_id=current_generation,
    )
    _require(
        int(orphan_report.orphan_count) == 0,
        "terminal_orphan_nodes_present",
        str(orphan_report.orphan_count),
    )
    _require(
        not dict(orphan_report.unresolved_reasons),
        "terminal_orphan_scan_unresolved",
        repr(orphan_report.unresolved_reasons),
    )
    live_graph = Path(os.environ["KG_BASE_DIR"]) / "boards" / board_id / "graph.lbug"
    _require(live_graph.is_file(), "terminal_graph_storage_missing", str(live_graph))
    post_health = await _real_health(composition, board_id)
    _require(
        str(post_health.get("graph_state") or "") == "healthy",
        "terminal_graph_health_not_healthy",
        str(post_health.get("graph_state")),
    )
    _require(
        str(post_health.get("current_kg_generation_id") or "")
        == str(current_generation),
        "terminal_health_generation_mismatch",
    )
    from okto_pulse.community.adapters.kuzu_graph_store import CommunityKuzuGraphStore

    graph_schema_version = CommunityKuzuGraphStore().get_schema_version(board_id)
    _require(bool(graph_schema_version), "terminal_graph_query_failed")
    quarantine_id = _assert_governed_quarantine(
        quarantine_root=quarantine_root,
        baseline_hashes=quarantine_baseline,
        baseline_ids=quarantine_baseline_ids,
        receipt=receipt_by_effect["quarantine"],
        board_id=board_id,
        manifest_ref=manifest.manifest_ref,
    )
    confirmation_receipt_ref = _assert_confirmation_receipt(
        bundle,
        board_id=board_id,
        actor_id=actor_id,
        manifest=manifest,
        run_id=str(result.run_id),
        preflight_hash=preflight_hash,
        confirmation_ref=confirmation_ref,
        user_reason=user_reason,
    )
    _assert_rebuild_artifacts_governed(
        rebuild_root=rebuild_root,
        baseline=rebuild_baseline,
        board_id=board_id,
        actor_id=actor_id,
        manifest_ref=manifest.manifest_ref,
        run_id=str(result.run_id),
        generation_id=str(current_generation),
        report_id=str(report_id),
        report_ref=str(report_ref),
        preflight_hash=preflight_hash,
        confirmation_ref=confirmation_ref,
        confirmation_receipt_ref=confirmation_receipt_ref,
        resume_mode=resume_mode,
        overlay_revision_baseline=overlay_revision_baseline,
        expect_overlay_revision_advance=expect_overlay_revision_advance,
        expected_cognitive_source_rows=expected_cognitive_source_rows,
        cognitive_ledger_baseline=cognitive_ledger_baseline,
        frozen_confirmation_receipt_baseline=(frozen_confirmation_receipt_baseline),
        rebaseline_audit_baseline=rebaseline_audit_baseline,
        expected_rebaseline_evidence=expected_rebaseline_evidence,
    )
    return {
        "run_id": result.run_id,
        "manifest_ref": manifest.manifest_ref,
        "source_set_hash": manifest.source_set_hash,
        "current_kg_generation_id": current_generation,
        "report_ref": report_ref,
        "report_id": report_id,
        "publishable_status": getattr(result, "publishable_status", None),
        "promotion_outcome": result.promotion_outcome,
        "event_emitted": result.event_emitted,
        "orphan_count": orphan_report.orphan_count,
        "quarantine_id": quarantine_id,
        "report_source_hash": expected_source_hash,
        "graph_schema_version": graph_schema_version,
    }


async def _shutdown_composed_runtime(
    composition: Any,
    processor: Any | None,
) -> None:
    from okto_pulse.community.adapters.kg_shutdown import close_all_graphs_on_shutdown
    from okto_pulse.community.adapters.sqlalchemy_database import close_db
    from okto_pulse.core.application.kg_events_hub import shutdown_kg_events_hub
    from okto_pulse.core.application.kg_runtime_access import resolve_graph_lifecycle

    if processor is not None:
        while True:
            pending = int(await processor._blocking_execution.join(timeout=30.0))
            if pending == 0:
                break
            _emit(
                "native_graph_shutdown_watchdog",
                pending=pending,
                teardown_deferred=True,
            )
    _assert_worker_registry_never_started(composition)
    errors: list[str] = []
    summary: Mapping[str, Any] = {}
    try:
        await shutdown_kg_events_hub()
    except BaseException as exc:
        errors.append(f"events:{type(exc).__name__}:{exc}")
    try:
        await resolve_graph_lifecycle().close(None)
    except BaseException as exc:
        errors.append(f"lifecycle:{type(exc).__name__}:{exc}")
    try:
        summary = await asyncio.to_thread(close_all_graphs_on_shutdown)
        if int(summary.get("boards_failed", 0)) != 0:
            errors.append(f"graphs:{summary!r}")
    except BaseException as exc:
        errors.append(f"graphs:{type(exc).__name__}:{exc}")
    try:
        await close_db()
    except BaseException as exc:
        errors.append(f"database:{type(exc).__name__}:{exc}")
    _require(not errors, "composed_runtime_shutdown_failed", repr(errors))
    _emit("composed_runtime_closed", **summary)


async def _execute_under_serve_lock(
    args: argparse.Namespace,
    *,
    data_home: Path,
    db_path: Path,
    owner_id: str,
    schema_fingerprint: str,
    serve_lock: Any,
    heartbeat: ServeLockHeartbeat,
    require_fresh: bool,
) -> dict[str, Any]:
    from okto_pulse.community.adapters.kg_events import (
        register_community_kg_events_reader,
    )
    from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
    from okto_pulse.community.adapters.sqlalchemy_runtime_settings_service import (
        apply_persisted_settings_to_core_settings,
    )
    from okto_pulse.community.main import create_community_app
    from okto_pulse.core.composition import runtime_composition_scope

    cancel_event = threading.Event()
    processor: Any | None = None
    app: Any | None = None
    transaction: Any | None = None
    service_task: asyncio.Task[Any] | None = None
    previous_handlers: dict[int, Any] = {}

    def request_cancel(_signum: int, _frame: Any) -> None:
        cancel_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(ValueError, OSError):
            previous_handlers[signum] = signal.signal(signum, request_cancel)

    try:
        # Raw filesystem proof must precede Community composition and every
        # artifact-store read: those readers are allowed to clean orphan
        # atomic-write temps. Refusing first preserves crash-cut evidence and
        # prevents a destructive run followed only by a late baseline failure.
        rebuild_root = data_home / "rebuild"
        rebuild_baseline = _snapshot_tree_hashes(rebuild_root)
        _assert_no_rebuild_transients(rebuild_baseline)
        # Capture the graph bytes before Community composition and, critically,
        # before any native health/open call. Ladybug open is not physically
        # read-only and keeps a Windows file handle in the Database cache.
        board_storage_root = data_home / "boards" / args.board_id
        board_storage_baseline = _snapshot_tree_hashes(board_storage_root)
        app = create_community_app()
        composition = app.state.runtime_composition
        transaction = app.state.mcp_cold_start_transaction
        _require(transaction is not None, "cold_start_transaction_missing")
        _assert_worker_registry_never_started(composition)
        with runtime_composition_scope(composition):
            _assert_full_orm_schema_present(db_path)
            _assert_schema_unchanged(db_path, schema_fingerprint)
            await apply_persisted_settings_to_core_settings()
            register_community_kg_events_reader(get_session_factory())
            _assert_worker_registry_never_started(composition)
            _require(
                not composition.scheduler_control.is_available()
                and getattr(composition.scheduler_control, "_scheduler", None) is None,
                "scheduler_started_unexpectedly",
            )
            process_probe = OfflineProcessProbe()

            def lifetime_probe() -> bool:
                try:
                    return bool(
                        heartbeat.failure is None
                        and _authoritative_serve_lock_matches(
                            serve_lock,
                            data_home=data_home,
                        )
                        and not any(
                            _port_is_listening(port) for port in args.offline_ports
                        )
                        and process_probe.is_offline()
                        and not composition.scheduler_control.is_available()
                        and getattr(composition.scheduler_control, "_scheduler", None)
                        is None
                        and tuple(composition.worker_registry.active_families) == ()
                        and all(
                            composition.worker_registry.start_count(family) == 0
                            for family in composition.worker_registry.families
                        )
                    )
                except BaseException:
                    return False

            _assert_no_pulse_processes()
            _require(lifetime_probe(), "offline_lifetime_proof_failed")
            await _authorize_governed_rebuild(
                composition,
                board_id=args.board_id,
                actor_id=owner_id,
            )
            _require(lifetime_probe(), "offline_lifetime_lost_during_authorization")
            quarantine_root = data_home / "quarantine"

            def legacy_evidence_probe(intent: Any) -> bool:
                return _assert_legacy_queue_only_evidence_current(
                    intent,
                    data_home=data_home,
                    db_path=db_path,
                )

            bundle = _build_service_bundle(
                legacy_evidence_probe=legacy_evidence_probe,
                legacy_db_path=db_path,
            )
            raw_health = _offline_cold_graph_health(
                bundle,
                board_id=args.board_id,
                board_storage_root=board_storage_root,
                board_storage_snapshot=board_storage_baseline,
            )
            _require(
                str(raw_health.get("graph_state") or "") != "quarantined",
                "rebuild_health_quarantined",
            )
            _require(
                bundle.single_writer_lock.inspect(board_id=args.board_id) is None,
                "preexisting_writer_lock_present",
            )
            _require(
                bundle.operation_reservation.inspect(board_id=args.board_id) is None,
                "preexisting_administrative_reservation_present",
            )
            overlay_revision_path = rebuild_root / _COGNITIVE_OVERLAY_REVISION_RELATIVE
            overlay_revision_baseline: Mapping[str, Any] | None = None
            if _COGNITIVE_OVERLAY_REVISION_RELATIVE in rebuild_baseline:
                overlay_revision_baseline = _load_json_file(
                    overlay_revision_path,
                    "baseline_cognitive_overlay_revision_invalid",
                )
                _validated_cognitive_overlay_revision(
                    overlay_revision_baseline,
                    code="baseline_cognitive_overlay_revision",
                )
                _require(
                    hashlib.sha256(overlay_revision_path.read_bytes()).hexdigest()
                    == rebuild_baseline[_COGNITIVE_OVERLAY_REVISION_RELATIVE],
                    "baseline_cognitive_overlay_revision_changed_while_reading",
                )
            else:
                _require(
                    not overlay_revision_path.exists()
                    and not overlay_revision_path.is_symlink(),
                    "baseline_cognitive_overlay_revision_untracked",
                )
            rebaseline_audit_baseline: Mapping[str, Any] | None = None
            rebaseline_audit_path = rebuild_root / _REBASELINE_AUDIT_RELATIVE
            if _REBASELINE_AUDIT_RELATIVE in rebuild_baseline:
                rebaseline_audit_baseline = _load_json_file(
                    rebaseline_audit_path,
                    "baseline_rebaseline_audit_invalid",
                )
                _require(
                    hashlib.sha256(rebaseline_audit_path.read_bytes()).hexdigest()
                    == rebuild_baseline[_REBASELINE_AUDIT_RELATIVE],
                    "baseline_rebaseline_audit_changed_while_reading",
                )
            else:
                _require(
                    not rebaseline_audit_path.exists()
                    and not rebaseline_audit_path.is_symlink(),
                    "baseline_rebaseline_audit_untracked",
                )
            quarantine_baseline = _snapshot_tree_hashes(quarantine_root)
            quarantine_baseline_ids = _quarantine_ids(quarantine_root)
            legacy_reconciliation = _discover_legacy_queue_only_reconciliation(
                bundle,
                data_home=data_home,
                db_path=db_path,
                rebuild_root=rebuild_root,
                rebuild_baseline=rebuild_baseline,
                quarantine_root=quarantine_root,
                quarantine_baseline=quarantine_baseline,
                board_storage_baseline=board_storage_baseline,
                board_id=args.board_id,
                recovery_actor_id=owner_id,
                recovery_reason=str(args.reason),
            )
            if legacy_reconciliation is None:
                plan = _select_recovery_run_plan(
                    bundle,
                    board_id=args.board_id,
                    actor_id=owner_id,
                    raw_health=raw_health,
                )
            elif legacy_reconciliation.terminal:
                _require(
                    legacy_reconciliation.adoption is not None,
                    "legacy_queue_only_terminal_adoption_missing",
                )
                preflight, fresh_manifest, fresh_source_set = (
                    _create_preflight_and_manifest(
                        bundle,
                        board_id=args.board_id,
                        raw_health=raw_health,
                    )
                )
                plan = RecoveryRunPlan(
                    mode="fresh",
                    manifest=fresh_manifest,
                    source_set=fresh_source_set,
                    preflight_hash=str(preflight.preflight_hash),
                    compensated_queue_adoption=legacy_reconciliation.adoption,
                    legacy_queue_only=legacy_reconciliation,
                )
            else:
                plan = RecoveryRunPlan(
                    mode="legacy_manual_restore_queue_only",
                    manifest=None,
                    source_set=None,
                    preflight_hash="",
                    legacy_queue_only=legacy_reconciliation,
                )
            if plan.mode == "legacy_manual_restore_queue_only":
                _require(
                    not require_fresh,
                    "reconciliation_lane_budget_exhausted",
                )
                _require(
                    plan.legacy_queue_only is not None,
                    "legacy_queue_only_plan_missing",
                )
                return await _run_legacy_queue_only_lane(
                    plan.legacy_queue_only,
                    bundle=bundle,
                    composition=composition,
                    db_path=db_path,
                    schema_fingerprint=schema_fingerprint,
                    rebuild_root=rebuild_root,
                    rebuild_baseline=rebuild_baseline,
                    quarantine_root=quarantine_root,
                    quarantine_baseline=quarantine_baseline,
                    board_storage_root=board_storage_root,
                    board_storage_baseline=board_storage_baseline,
                    actor_id=owner_id,
                    cancel_event=cancel_event,
                    lifetime_probe=lifetime_probe,
                    timeout_seconds=args.run_timeout_seconds,
                )
            closed_baseline_kind: str | None = None
            if plan.mode == "fresh_pending_terminal_proof":
                prior_receipt = plan.previous_terminal_receipt
                prior_audit = plan.previous_terminal_audit
                _require(
                    isinstance(prior_receipt, Mapping)
                    and isinstance(prior_audit, Mapping),
                    "previous_terminal_proof_plan_incomplete",
                )
                assert isinstance(prior_receipt, Mapping)
                assert isinstance(prior_audit, Mapping)
                from okto_pulse.core.kg.rebuild_service import (
                    is_rebuild_terminal_audit_frozen,
                )

                compensated_adoption: CompensatedQueueAdoption | None = None
                if is_rebuild_terminal_audit_frozen(prior_audit):
                    closed_baseline_kind = "prior_frozen_terminal"
                else:
                    closed_baseline_kind, compensated_adoption = (
                        _assert_closed_operation_baseline_safe(
                            bundle,
                            receipt=prior_receipt,
                            audit=prior_audit,
                            board_id=args.board_id,
                            db_path=db_path,
                            rebuild_root=rebuild_root,
                            rebuild_baseline=rebuild_baseline,
                            quarantine_root=quarantine_root,
                            quarantine_baseline=quarantine_baseline,
                            board_storage_root=board_storage_root,
                            raw_health=raw_health,
                        )
                    )
                preflight, fresh_manifest, fresh_source_set = (
                    _create_preflight_and_manifest(
                        bundle,
                        board_id=args.board_id,
                        raw_health=raw_health,
                    )
                )
                plan = replace(
                    plan,
                    mode="fresh",
                    manifest=fresh_manifest,
                    source_set=fresh_source_set,
                    preflight_hash=str(preflight.preflight_hash),
                    compensated_queue_adoption=compensated_adoption,
                )
            elif plan.mode == "archive_closed":
                _require(
                    isinstance(plan.receipt, Mapping)
                    and isinstance(plan.terminal_audit, Mapping),
                    "archive_closed_plan_proof_missing",
                )
                assert isinstance(plan.receipt, Mapping)
                assert isinstance(plan.terminal_audit, Mapping)
                closed_baseline_kind, _closed_adoption = (
                    _assert_closed_operation_baseline_safe(
                        bundle,
                        receipt=plan.receipt,
                        audit=plan.terminal_audit,
                        board_id=args.board_id,
                        db_path=db_path,
                        rebuild_root=rebuild_root,
                        rebuild_baseline=rebuild_baseline,
                        quarantine_root=quarantine_root,
                        quarantine_baseline=quarantine_baseline,
                        board_storage_root=board_storage_root,
                        raw_health=raw_health,
                    )
                )
            _require(
                not require_fresh or plan.mode == "fresh",
                "reconciliation_did_not_converge_to_fresh",
                plan.mode,
            )
            cognitive_ledger_baseline = _capture_cognitive_ledger_baseline(
                rebuild_root=rebuild_root,
                rebuild_baseline=rebuild_baseline,
                board_id=args.board_id,
            )
            manifest = plan.manifest
            compensation_mode = plan.mode == "resume_compensation"
            archive_closed_mode = plan.mode == "archive_closed"
            reconciliation_mode = compensation_mode or archive_closed_mode
            _require(manifest is not None, "recovery_plan_manifest_missing", plan.mode)
            if not reconciliation_mode:
                _require(
                    plan.source_set is not None,
                    "recovery_plan_source_set_missing",
                    plan.mode,
                )
            manifest_artifact_hash = ""
            if not reconciliation_mode:
                rebaseline_evidence_id = (
                    f"{plan.run_id}:{manifest.manifest_ref}"
                    if plan.rebaseline_evidence is not None and plan.run_id
                    else None
                )
                rebaseline_target_source_set_hash = (
                    str(plan.rebaseline_evidence["to_source_set_hash"])
                    if plan.rebaseline_evidence is not None
                    else None
                )
                if rebaseline_target_source_set_hash is not None:
                    _require(
                        re.fullmatch(r"[0-9a-f]{64}", rebaseline_target_source_set_hash)
                        is not None,
                        "recovery_plan_rebaseline_target_hash_invalid",
                    )
                rebaseline_materializable_sources = (
                    tuple(
                        row.to_dict() for row in plan.source_set.materializable_sources
                    )
                    if rebaseline_evidence_id is not None
                    and plan.source_set is not None
                    else None
                )
                bundle.event_manifest_bindings[manifest.manifest_ref] = (
                    EventManifestBinding(
                        board_id=args.board_id,
                        preflight_hash=plan.preflight_hash,
                        rebaseline_evidence_id=rebaseline_evidence_id,
                        rebaseline_target_source_set_hash=(
                            rebaseline_target_source_set_hash
                        ),
                        rebaseline_materializable_sources=(
                            rebaseline_materializable_sources
                        ),
                    )
                )
                _manifest_reference, manifest_artifact_hash = (
                    _manifest_artifact_fingerprint(
                        bundle,
                        manifest_ref=manifest.manifest_ref,
                    )
                )
            resume_checkpoint_baseline = (
                _load_checkpoint(bundle, args.board_id, manifest.manifest_ref)
                if plan.mode not in {"fresh", "archive_closed"}
                else None
            )
            _require(
                resume_checkpoint_baseline is None
                or isinstance(resume_checkpoint_baseline, Mapping),
                "authorized_resume_checkpoint_invalid",
            )
            expected_compensation_board_storage = (
                _expected_compensation_board_storage(
                    resume_checkpoint_baseline,
                    quarantine_root=quarantine_root,
                    board_id=args.board_id,
                    manifest_ref=manifest.manifest_ref,
                )
                if compensation_mode
                else {}
            )
            if archive_closed_mode:
                expected_source_rows = ()
                dependency_closure_count = 0
                ordered_source_rows = ()
            elif compensation_mode:
                expected_source_rows = _resume_checkpoint_source_rows(
                    resume_checkpoint_baseline,
                    board_id=args.board_id,
                    manifest_ref=manifest.manifest_ref,
                )
                dependency_closure_count = 0
                ordered_source_rows: tuple[Mapping[str, Any], ...] = ()
            else:
                expected_source_rows, dependency_closure_count = (
                    _resolve_expected_command_sources(
                        db_path,
                        board_id=args.board_id,
                        manifest=manifest,
                        rebaseline_source_set=(
                            plan.source_set
                            if plan.rebaseline_evidence is not None
                            else None
                        ),
                        rebaseline_evidence_id=rebaseline_evidence_id,
                    )
                )
                ordered_source_rows = _production_ordered_source_rows(
                    db_path,
                    board_id=args.board_id,
                    source_rows=expected_source_rows,
                )
            admitted_identities = _expected_queue_keys(expected_source_rows)
            if not reconciliation_mode:
                _assert_protected_admission_is_noop(
                    db_path,
                    board_id=args.board_id,
                    admitted_identities=admitted_identities,
                    authorized_rebuild_source=f"rebuild:{manifest.manifest_ref}",
                    compensated_adoption=plan.compensated_queue_adoption,
                )
            pre_admission_protected_queue = _protected_queue_snapshot(
                db_path,
                board_id=args.board_id,
                admitted_identities=admitted_identities,
            )
            initial_dlq = _dlq_snapshot(db_path)
            initial_board_dlq_ids = _dlq_ids_for_board(db_path, args.board_id)
            logical_database_baseline: Mapping[str, str] = {}
            compensation_queue_baseline: QueueSnapshot | None = None
            _assert_no_pulse_processes()
            _require(lifetime_probe(), "offline_lifetime_proof_failed")
            capability_stack = ExitStack()
            recovery_capability = capability_stack.enter_context(
                _recovery_capability_context(
                    board_id=args.board_id,
                    lifetime_probe=lifetime_probe,
                )
            )
            fresh_confirmation: tuple[str, str, str, str, str, str] | None = None
            try:
                if plan.mode == "fresh":
                    bound_reason = str(args.reason)
                    token = bundle.confirmation_store.issue(
                        board_id=args.board_id,
                        actor_id=owner_id,
                        operation=OPERATION,
                        preflight_hash=plan.preflight_hash,
                        manifest_ref=manifest.manifest_ref,
                    )
                    fresh_confirmation = (
                        token.confirmation_id,
                        args.board_id,
                        owner_id,
                        OPERATION,
                        plan.preflight_hash,
                        manifest.manifest_ref,
                    )
                    from okto_pulse.core.kg.rebuild_audit import (
                        confirmation_fingerprint,
                    )

                    confirmation_ref = confirmation_fingerprint(token.confirmation_id)
                    service_callable = bundle.service.run
                    service_kwargs = {
                        "confirmation_id": token.confirmation_id,
                        "board_id": args.board_id,
                        "actor_id": owner_id,
                        "operation": OPERATION,
                        "preflight_hash": plan.preflight_hash,
                        "manifest_ref": manifest.manifest_ref,
                        "reason": bound_reason,
                        "cancel_requested": cancel_event.is_set,
                        "recovery_capability": recovery_capability,
                    }
                    event_name = "governed_rebuild_issued"
                else:
                    _require(
                        bool(plan.run_id)
                        and bool(plan.confirmation_ref)
                        and bool(plan.user_reason),
                        "authorized_resume_plan_incomplete",
                    )
                    confirmation_ref = str(plan.confirmation_ref)
                    bound_reason = str(plan.user_reason)
                    service_callable = bundle.service.resume_authorized_run
                    service_kwargs = {
                        "run_id": str(plan.run_id),
                        "board_id": args.board_id,
                        "actor_id": owner_id,
                        "operation": OPERATION,
                        "preflight_hash": plan.preflight_hash,
                        "manifest_ref": manifest.manifest_ref,
                        "reason": bound_reason,
                        "cancel_requested": cancel_event.is_set,
                        "recovery_capability": recovery_capability,
                    }
                    event_name = "governed_rebuild_resume_selected"
                _emit(
                    event_name,
                    board_id=args.board_id,
                    manifest_ref=manifest.manifest_ref,
                    source_set_hash=manifest.source_set_hash,
                    eligible_source_count=(
                        int(plan.source_set.eligible_count)
                        if plan.source_set is not None
                        else None
                    ),
                    dependency_closure_count=dependency_closure_count,
                    confirmation_ref=confirmation_ref,
                    reason_binding=("fresh_cli" if plan.mode == "fresh" else "receipt"),
                    manifest_verification_failure=plan.manifest_verification_failure,
                )
                if archive_closed_mode:
                    # Capture after all read-only selection and immediately
                    # before Core acquires its short-lived reservation/lock.
                    logical_database_baseline = _sqlite_logical_fingerprints(db_path)
                elif compensation_mode:
                    compensation_queue_baseline = _full_queue_snapshot(db_path)
                    logical_database_baseline = _sqlite_logical_fingerprints(
                        db_path,
                        exclude_tables=frozenset({"consolidation_queue"}),
                    )
                service_task = asyncio.create_task(
                    asyncio.to_thread(
                        service_callable,
                        **service_kwargs,
                    ),
                    name="offline-kg-rebuild-service",
                )
                if archive_closed_mode:
                    result = await _await_closed_archive_reconciliation(
                        service_task,
                        bundle,
                        plan,
                        composition,
                        board_id=args.board_id,
                        db_path=db_path,
                        rebuild_root=rebuild_root,
                        rebuild_baseline=rebuild_baseline,
                        board_storage_root=board_storage_root,
                        board_storage_baseline=board_storage_baseline,
                        quarantine_root=quarantine_root,
                        quarantine_baseline=quarantine_baseline,
                        baseline_queue=pre_admission_protected_queue,
                        baseline_dlq=initial_dlq,
                        logical_database_baseline=logical_database_baseline,
                        baseline_health=raw_health,
                        cancel_event=cancel_event,
                        lifetime_probe=lifetime_probe,
                        timeout_seconds=args.run_timeout_seconds,
                        poll_seconds=args.poll_seconds,
                    )
                    service_task = None
                    _assert_schema_unchanged(db_path, schema_fingerprint)
                    _assert_worker_registry_never_started(composition)
                    _emit(
                        "closed_receipt_reconciled",
                        board_id=args.board_id,
                        run_id=result["reconciled_run_id"],
                        baseline_kind=closed_baseline_kind,
                    )
                    return result
                if compensation_mode:
                    _require(
                        isinstance(manifest, AuthorizedManifestBinding),
                        "manifest_compensation_binding_missing",
                    )
                    result = await _await_manifest_compensation(
                        service_task,
                        bundle,
                        manifest,
                        board_id=args.board_id,
                        db_path=db_path,
                        baseline_dlq=initial_dlq,
                        checkpoint_baseline=resume_checkpoint_baseline,
                        cancel_event=cancel_event,
                        lifetime_probe=lifetime_probe,
                        timeout_seconds=args.run_timeout_seconds,
                        poll_seconds=args.poll_seconds,
                    )
                    service_task = None
                    _require(
                        compensation_queue_baseline is not None,
                        "manifest_compensation_queue_baseline_missing",
                    )
                    assert compensation_queue_baseline is not None
                    await _assert_manifest_compensation_reconciliation(
                        bundle,
                        plan,
                        result,
                        composition,
                        board_id=args.board_id,
                        db_path=db_path,
                        rebuild_root=rebuild_root,
                        rebuild_baseline=rebuild_baseline,
                        quarantine_root=quarantine_root,
                        quarantine_baseline=quarantine_baseline,
                        quarantine_baseline_ids=quarantine_baseline_ids,
                        board_storage_root=board_storage_root,
                        board_storage_baseline=board_storage_baseline,
                        expected_board_storage=expected_compensation_board_storage,
                        baseline_queue=compensation_queue_baseline,
                        baseline_dlq=initial_dlq,
                        logical_database_baseline=logical_database_baseline,
                        baseline_health=raw_health,
                        checkpoint_baseline=resume_checkpoint_baseline,
                    )
                    _assert_schema_unchanged(db_path, schema_fingerprint)
                    _assert_worker_registry_never_started(composition)
                    _emit(
                        "authorized_resume_manifest_compensated",
                        board_id=args.board_id,
                        run_id=result.run_id,
                        manifest_ref=manifest.manifest_ref,
                        outcome=result.outcome,
                    )
                    return {
                        "_recovery_phase": "reconciled",
                        "reconciliation_kind": "manifest_compensation",
                        "reconciled_run_id": str(result.run_id),
                    }
                (
                    checkpoint,
                    early_result,
                    requires_claims,
                ) = await _wait_for_admission_or_post_drain(
                    service_task,
                    bundle,
                    board_id=args.board_id,
                    manifest_ref=manifest.manifest_ref,
                    timeout_seconds=args.admission_timeout_seconds,
                    poll_seconds=args.poll_seconds,
                )
                _require(lifetime_probe(), "offline_lifetime_lost_at_admission")
                if requires_claims:
                    _assert_reservation_exact(
                        bundle,
                        board_id=args.board_id,
                        manifest_ref=manifest.manifest_ref,
                    )
                    _require(
                        bundle.single_writer_lock.inspect(board_id=args.board_id)
                        is None,
                        "writer_a_not_released_before_drain",
                    )
                source_rows = _assert_admission_checkpoint(
                    checkpoint,
                    board_id=args.board_id,
                    manifest_ref=manifest.manifest_ref,
                    initial_board_dlq_ids=initial_board_dlq_ids,
                    require_draining=requires_claims,
                )
                _require(
                    _canonical_source_rows(source_rows)
                    == _canonical_source_rows(expected_source_rows),
                    "checkpoint_dependency_closure_mismatch",
                )
                source = f"rebuild:{manifest.manifest_ref}"
                claim_scope: Any | None = None
                if requires_claims:
                    from okto_pulse.core.application.processors.consolidation import (
                        ConsolidationProcessor,
                    )
                    from okto_pulse.core.ports.consolidation import (
                        ConsolidationClaimScope,
                    )

                    processor = ConsolidationProcessor(batch_size=args.batch_size)
                    claim_scope = ConsolidationClaimScope(
                        board_id=args.board_id,
                        source=source,
                        work_kind="consolidate",
                    )
                exact_rows = _exact_queue_rows(
                    db_path,
                    board_id=args.board_id,
                    source=source,
                )
                claimed_count = 0
                if requires_claims:
                    claimed_count = _assert_exact_queue_admission(
                        exact_rows,
                        manifest_ref=manifest.manifest_ref,
                        source_rows=source_rows,
                        ordered_source_rows=ordered_source_rows,
                        allow_consumed_prefix=plan.mode == "resume",
                        allow_claimed_recovery=plan.mode == "resume",
                    )
                else:
                    _require(
                        _active_exact_queue_depth(
                            db_path,
                            board_id=args.board_id,
                            source=source,
                        )
                        == 0,
                        "post_drain_resume_has_active_exact_rows",
                    )
                _assert_unchanged_snapshot(
                    _dlq_snapshot(db_path),
                    initial_dlq,
                    "dead_letter_set_changed_before_drain",
                )
                non_target = _protected_queue_snapshot(
                    db_path,
                    board_id=args.board_id,
                    admitted_identities=admitted_identities,
                )
                _assert_unchanged_snapshot(
                    non_target,
                    pre_admission_protected_queue,
                    "protected_queue_changed_during_admission",
                )
                _assert_source_unchanged(bundle, manifest, args.board_id)
                if claimed_count:
                    _require(
                        plan.mode == "resume"
                        and processor is not None
                        and claim_scope is not None,
                        "exact_claim_recovery_requires_resume",
                    )
                    await _recover_exact_claims_for_resume(
                        processor,
                        claim_scope,
                        bundle,
                        manifest,
                        board_id=args.board_id,
                        db_path=db_path,
                        source=source,
                        claimed_count=claimed_count,
                        source_rows=source_rows,
                        ordered_source_rows=ordered_source_rows,
                        baseline_dlq=initial_dlq,
                        baseline_non_target=non_target,
                        admitted_identities=admitted_identities,
                        cancel_event=cancel_event,
                        lifetime_probe=lifetime_probe,
                    )

                if early_result is not None:
                    result = early_result
                elif requires_claims:
                    _require(
                        processor is not None and claim_scope is not None,
                        "exact_claim_processor_missing",
                    )
                    result = await _drain_exact_scope(
                        service_task,
                        processor,
                        claim_scope,
                        bundle,
                        manifest,
                        board_id=args.board_id,
                        source=source,
                        baseline_dlq=initial_dlq,
                        baseline_non_target=non_target,
                        admitted_identities=admitted_identities,
                        cancel_event=cancel_event,
                        lifetime_probe=lifetime_probe,
                        timeout_seconds=args.run_timeout_seconds,
                        poll_seconds=args.poll_seconds,
                    )
                else:
                    result = await _await_terminal_without_claims(
                        service_task,
                        bundle,
                        manifest,
                        board_id=args.board_id,
                        baseline_dlq=initial_dlq,
                        baseline_non_target=non_target,
                        admitted_identities=admitted_identities,
                        cancel_event=cancel_event,
                        lifetime_probe=lifetime_probe,
                        timeout_seconds=args.run_timeout_seconds,
                        poll_seconds=args.poll_seconds,
                    )
                service_task = None
                _require(lifetime_probe(), "offline_lifetime_lost_before_terminal_gate")
                terminal = await _assert_terminal_gates(
                    composition,
                    bundle,
                    manifest,
                    result,
                    board_id=args.board_id,
                    actor_id=owner_id,
                    db_path=db_path,
                    source=source,
                    baseline_dlq=initial_dlq,
                    baseline_non_target=non_target,
                    admitted_identities=admitted_identities,
                    expected_source_rows=expected_source_rows,
                    expected_cognitive_source_rows=(
                        tuple(
                            row.to_dict()
                            for row in plan.source_set.materializable_sources
                        )
                        if plan.rebaseline_evidence is not None
                        and not plan.frozen_terminal
                        and plan.source_set is not None
                        else tuple(
                            row.to_dict() for row in manifest.materializable_sources
                        )
                    ),
                    quarantine_root=quarantine_root,
                    quarantine_baseline=quarantine_baseline,
                    quarantine_baseline_ids=quarantine_baseline_ids,
                    rebuild_root=rebuild_root,
                    rebuild_baseline=rebuild_baseline,
                    preflight_hash=plan.preflight_hash,
                    confirmation_ref=confirmation_ref,
                    user_reason=bound_reason,
                    manifest_artifact_hash=manifest_artifact_hash,
                    resume_mode=plan.mode == "resume",
                    resume_checkpoint_baseline=resume_checkpoint_baseline,
                    overlay_revision_baseline=overlay_revision_baseline,
                    expect_overlay_revision_advance=not plan.frozen_terminal,
                    cognitive_ledger_baseline=cognitive_ledger_baseline,
                    frozen_confirmation_receipt_baseline=(
                        plan.receipt if plan.frozen_terminal else None
                    ),
                    rebaseline_audit_baseline=rebaseline_audit_baseline,
                    expected_rebaseline_evidence=(plan.rebaseline_evidence),
                )
                _assert_schema_unchanged(db_path, schema_fingerprint)
                _assert_worker_registry_never_started(composition)
                return terminal
            finally:
                if service_task is not None:
                    await _await_service_fenced(
                        service_task,
                        cancel_event=cancel_event,
                    )
                    service_task = None
                try:
                    if fresh_confirmation is not None:
                        (
                            confirmation_id,
                            expected_board_id,
                            expected_actor_id,
                            expected_operation,
                            expected_preflight_hash,
                            expected_manifest_ref,
                        ) = fresh_confirmation
                        revoked = bundle.confirmation_store.revoke_unconsumed(
                            confirmation_id=confirmation_id,
                            expected_board_id=expected_board_id,
                            expected_actor_id=expected_actor_id,
                            expected_operation=expected_operation,
                            expected_preflight_hash=expected_preflight_hash,
                            expected_manifest_ref=expected_manifest_ref,
                        )
                        if revoked:
                            from okto_pulse.core.kg.rebuild_audit import (
                                confirmation_fingerprint,
                            )

                            _emit(
                                "unconsumed_rebuild_confirmation_revoked",
                                confirmation_ref=confirmation_fingerprint(
                                    confirmation_id
                                ),
                            )
                        fresh_confirmation = None
                finally:
                    capability_stack.close()
    except BaseException:
        cancel_event.set()
        if service_task is not None:
            await _await_service_fenced(service_task, cancel_event=cancel_event)
            service_task = None
        raise
    finally:
        cancel_event.set()
        try:
            try:
                if app is not None:
                    composition = app.state.runtime_composition
                    from okto_pulse.core.composition import runtime_composition_scope

                    with runtime_composition_scope(composition):
                        await _shutdown_composed_runtime(composition, processor)
            finally:
                if transaction is not None:
                    transaction.rollback()
                    _require(
                        bool(getattr(transaction, "_rolled_back", False)),
                        "cold_start_transaction_not_rolled_back",
                    )
        finally:
            for signum, previous in previous_handlers.items():
                with suppress(ValueError, OSError):
                    signal.signal(signum, previous)


def _run_bounded_recovery_lanes(
    run_lane: Callable[[bool], dict[str, Any]],
    *,
    validate_reconciliation_boundary: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    """Run at most one fenced reconciliation followed by one fresh lane."""

    first = run_lane(False)
    if first.get("_recovery_phase") != "reconciled":
        return first
    validate_reconciliation_boundary(first)
    second = run_lane(True)
    _require(
        second.get("_recovery_phase") != "reconciled",
        "reconciliation_lane_budget_exhausted",
    )
    return second


def _execute(args: argparse.Namespace, evidence: InstallEvidence) -> int:
    data_home = _resolve_explicit_data_home(args.data_home, require_existing=True)
    db_path = (data_home / "data" / "pulse.db").resolve()
    execution_contract = _recovery_execution_contract(args)
    expected_fingerprint = str(args.expected_install_fingerprint).lower()
    _require(
        evidence.fingerprint == expected_fingerprint,
        "installed_fingerprint_mismatch",
        f"expected={expected_fingerprint} actual={evidence.fingerprint}",
    )
    _assert_ports_offline(args.offline_ports)
    _assert_no_pulse_processes()
    rehearsal_source_home: Path | None = None
    rehearsal_source_snapshot: dict[str, str] | None = None
    rehearsal_source_schema: str | None = None
    rehearsal_receipt_out: Path | None = None
    live_receipt_path: Path | None = None
    live_consumed_receipt_path: Path | None = None
    live_receipt: dict[str, Any] | None = None
    live_receipt_file_hash: str | None = None
    if args.rehearsal_copy_of:
        rehearsal_source_home = _resolve_explicit_data_home(
            str(args.rehearsal_copy_of),
            require_existing=True,
        )
        rehearsal_source_snapshot, rehearsal_source_schema = _assert_rehearsal_copy(
            source_home=rehearsal_source_home,
            target_home=data_home,
            board_id=args.board_id,
        )
        rehearsal_receipt_out = _resolve_external_new_file(
            str(args.rehearsal_receipt_out),
            protected_homes=(data_home, rehearsal_source_home),
            purpose="rehearsal_receipt",
        )
    else:
        live_receipt_path = _resolve_external_existing_file(
            str(args.rehearsal_receipt),
            protected_homes=(data_home,),
            purpose="rehearsal_receipt",
        )
        live_receipt, live_receipt_file_hash = _validate_rehearsal_attestation(
            receipt_path=live_receipt_path,
            data_home=data_home,
            board_id=args.board_id,
            install_fingerprint=evidence.fingerprint,
            execution_contract=execution_contract,
        )
        live_consumed_receipt_path = _register_live_consumption(
            receipt_path=live_receipt_path,
            receipt=live_receipt,
            receipt_file_hash=live_receipt_file_hash,
            data_home=data_home,
        )
        _emit(
            "live_recovery_attestation_consumed",
            board_id=args.board_id,
            attestation_sha256=live_receipt["attestation_sha256"],
            consumed_receipt_sha256=hashlib.sha256(
                live_consumed_receipt_path.read_bytes()
            ).hexdigest(),
        )
    configured_db_path = _configure_explicit_environment(data_home)
    _require(configured_db_path == db_path, "configured_database_path_mismatch")
    sqlite_storage_before = _sqlite_storage_fingerprints(db_path)

    # Pulse imports occur only after the explicit environment is frozen.
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.serve_lock import (
        HEARTBEAT_INTERVAL_SECONDS,
        acquire_serve_lock,
    )

    settings = CommunitySettings(_env_file=None)
    _assert_settings_identity(settings, data_home, db_path)
    with acquire_serve_lock(settings) as serve_lock:
        _require(serve_lock is not None, "serve_lock_reentrant_refused")
        heartbeat = ServeLockHeartbeat(
            serve_lock,
            interval_seconds=max(1.0, HEARTBEAT_INTERVAL_SECONDS / 2),
        )
        heartbeat.start()
        try:
            if live_receipt_path is not None:
                assert live_consumed_receipt_path is not None
                assert live_receipt is not None
                assert live_receipt_file_hash is not None
                locked_receipt, locked_file_hash = _validate_rehearsal_attestation(
                    receipt_path=live_consumed_receipt_path,
                    bound_receipt_path=live_receipt_path,
                    data_home=data_home,
                    board_id=args.board_id,
                    install_fingerprint=evidence.fingerprint,
                    execution_contract=execution_contract,
                )
                _require(
                    locked_receipt == live_receipt
                    and locked_file_hash == live_receipt_file_hash,
                    "rehearsal_receipt_changed_after_consumption",
                )
            owner_id, _board_settings, schema_fingerprint = _assert_database_preflight(
                db_path,
                args.board_id,
            )
            _emit(
                "offline_recovery_started",
                board_id=args.board_id,
                data_home=str(data_home),
                install_fingerprint=evidence.fingerprint,
                platform=platform.platform(),
            )

            def run_lane(require_fresh: bool) -> dict[str, Any]:
                return asyncio.run(
                    _execute_under_serve_lock(
                        args,
                        data_home=data_home,
                        db_path=db_path,
                        owner_id=owner_id,
                        schema_fingerprint=schema_fingerprint,
                        serve_lock=serve_lock,
                        heartbeat=heartbeat,
                        require_fresh=require_fresh,
                    )
                )

            def validate_reconciliation_boundary(
                lane_result: Mapping[str, Any],
            ) -> None:
                _emit(
                    "offline_reconciliation_lane_completed",
                    board_id=args.board_id,
                    reconciliation_kind=lane_result.get("reconciliation_kind"),
                    reconciled_run_id=lane_result.get("reconciled_run_id"),
                )
                _assert_ports_offline(args.offline_ports)
                _assert_no_pulse_processes()
                (
                    next_owner_id,
                    _next_board_settings,
                    next_schema_fingerprint,
                ) = _assert_database_preflight(db_path, args.board_id)
                _require(
                    next_owner_id == owner_id
                    and next_schema_fingerprint == schema_fingerprint,
                    "reconciliation_lane_database_identity_changed",
                )
                _require(
                    heartbeat.failure is None
                    and _authoritative_serve_lock_matches(
                        serve_lock,
                        data_home=data_home,
                    ),
                    "reconciliation_lane_offline_fence_lost",
                )

            terminal = _run_bounded_recovery_lanes(
                run_lane,
                validate_reconciliation_boundary=validate_reconciliation_boundary,
            )
            (
                board_storage_post_teardown,
                board_storage_post_teardown_sha256,
            ) = _capture_post_teardown_board_storage(
                data_home=data_home,
                board_id=args.board_id,
            )
            terminal["board_storage_post_teardown"] = board_storage_post_teardown
            terminal["board_storage_post_teardown_sha256"] = (
                board_storage_post_teardown_sha256
            )
            sqlite_storage_after = _sqlite_storage_fingerprints(db_path)
            terminal["sqlite_storage_before"] = sqlite_storage_before
            terminal["sqlite_storage_after"] = sqlite_storage_after
            _require(heartbeat.failure is None, "serve_lock_heartbeat_failed")
        finally:
            heartbeat.stop()
    _require(heartbeat.failure is None, "serve_lock_heartbeat_failed")
    if rehearsal_source_home is not None:
        assert rehearsal_source_snapshot is not None
        assert rehearsal_source_schema is not None
        assert rehearsal_receipt_out is not None
        # The attestation is created only after processor/native/DB teardown,
        # heartbeat stop and serve-lock release have all succeeded.
        _assert_ports_offline(args.offline_ports)
        _assert_no_pulse_processes()
        terminal_source_snapshot = _rehearsal_scope_snapshot(
            rehearsal_source_home,
            args.board_id,
        )
        _require(
            terminal_source_snapshot == rehearsal_source_snapshot,
            "rehearsal_source_home_changed",
        )
        terminal_source_db = rehearsal_source_home / "data" / "pulse.db"
        terminal_source_schema = _sqlite_schema_fingerprint(terminal_source_db)
        terminal_source_storage = _sqlite_storage_fingerprints(terminal_source_db)
        _require(
            terminal_source_schema == rehearsal_source_schema,
            "rehearsal_source_schema_changed",
        )
        _require(
            _rehearsal_scope_snapshot(
                rehearsal_source_home,
                args.board_id,
            )
            == terminal_source_snapshot,
            "rehearsal_source_home_changed_during_attestation",
        )
        terminal["rehearsal_source_unchanged"] = True
        terminal["rehearsal_source_home"] = str(rehearsal_source_home)
        attestation = _build_rehearsal_attestation(
            board_id=args.board_id,
            source_home=rehearsal_source_home,
            source_snapshot=terminal_source_snapshot,
            source_schema=terminal_source_schema,
            source_storage=terminal_source_storage,
            terminal=terminal,
            install_fingerprint=evidence.fingerprint,
            receipt_path=rehearsal_receipt_out,
            execution_contract=execution_contract,
        )
        _write_new_json_file(
            rehearsal_receipt_out,
            attestation,
            code="rehearsal_receipt",
        )
        receipt_bytes = _read_bounded_stable_file(
            rehearsal_receipt_out,
            code="rehearsal_receipt",
        )
        _require(
            _rehearsal_scope_snapshot(
                rehearsal_source_home,
                args.board_id,
            )
            == terminal_source_snapshot,
            "rehearsal_source_home_changed_after_attestation",
        )
        _emit(
            "rehearsal_attestation_written",
            board_id=args.board_id,
            attestation_sha256=attestation["attestation_sha256"],
            expires_at=attestation["expires_at"],
            receipt_file_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        )
    _emit("offline_recovery_completed", **terminal)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    evidence = _install_evidence(
        core_version=args.expected_core_version,
        community_version=args.expected_community_version,
        ladybug_version=args.expected_ladybug_version,
        kuzu_version=args.expected_kuzu_version,
        sqlalchemy_version=args.expected_sqlalchemy_version,
        aiosqlite_version=args.expected_aiosqlite_version,
    )
    if args.inspect_install:
        _emit(
            "installed_pulse_evidence",
            install_fingerprint=evidence.fingerprint,
            executor_sha256=_hash_executor_file(),
            entrypoints_sha256=_hash_recovery_entrypoint_metadata(),
            distributions=[asdict(item) for item in evidence.distributions],
            runtime=dict(evidence.runtime),
        )
        return 0
    try:
        return _execute(args, evidence)
    except RecoveryRefused as exc:
        _emit("offline_recovery_refused", error=str(exc))
        return 2
    except BaseException as exc:
        _emit(
            "offline_recovery_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
