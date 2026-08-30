"""Frente D / fase 0a — micro-trace por familia com FIXTURES MUTANTES sobre o board final do RUN #3.

Revision 3 — after Codex's rejection of v2.1 (7 blockers). What changed and why:

  BLOCKER 1  Re-running trace operations on the FINAL board tends to no-op (tombstones already
             applied, deletes-by-session with no rows, lineage/projection already reconciled),
             which would measure RC0 as zero exactly where it matters. v3 therefore BUILDS ITS
             OWN FIXTURES: private sessions prof-<seed>-*, private ids prof-<seed>-*, payload
             SHAPES taken from the frozen manifest (first operation of each family is the
             template), identities substituted. Every measured operation writes by construction
             and is followed by a READ-BACK POSTCONDITION; a failed postcondition fails the
             whole profile.
  BLOCKER 2  One COMMON operation set for both backends: fixtures derive from a fixed seed; the
             JSON carries the digest of the operation set.
  BLOCKER 3  The active storage root is resolved from the backend's own graph_backend_binding.json
             (backend field checked, physical_path must exist) — never rglob.
  BLOCKER 4  Each (family, pass) copy is released after its pass.
  BLOCKER 5  The forensic inventory digest is over file CONTENT (sha256 of every byte).
  BLOCKER 6  Every hook, including BufferPool._write_back, accumulates inclusive nanoseconds.
  BLOCKER 7  per_family > 0 is validated.

Revision h1-h8 (2026-08-30, branch perf/w0-harness-h1-h8, GRAFX_PERFORMANCE_NEXT_STEPS.md section
6.1). Versioned copy of the certified harness (sha256 0cb60d5e...); the RAW pass is LOGICALLY
UNCHANGED (no hooks, no cProfile, same open/close/timing sequence) and operation_set_sha256 is
unchanged for the default fixture plan, so RAW medians remain comparable with the pf5 reports.
Instrumented output is NOT comparable across harness revisions (hook set changed): compare
instrumented/phases passes only between reports whose ``harness_revision.sha256`` agree.

  H1  pstats dump sorted by ``tottime`` beside the cumulative top-N (``cprofile_top_tottime``).
  H2  Hooks corrected for per-statement attribution: Database._run_statement, QueryEngine.execute
      / _planned_for / planned, LocalProcessCoordinator._take_file_lock / _wait_for_os_lock /
      _drop_lock, os.close, msvcrt.locking, public_views._query_parameters_snapshot /
      _query_result_view. Context managers (page_access_section, _section) are NOT hooked: the
      ``counted`` wrapper returns before their body runs and would report ~0. Hook times are
      INCLUSIVE and nested: attribute by difference, never by summing nested keys. Targets whose
      inclusive time cannot be measured (generator/async functions) are listed under
      ``unmeasurable_targets``.
  H3  Third pass ``phases`` (RAW-phases): three timers per operation -- begin, each execute,
      commit/rollback -- installed on the backend INSTANCE (no hooks, no cProfile), published as
      ``passes.phases`` beside RAW and never in its place.
  H4  In the instrumented pass the hooks are installed BEFORE the open, so the open (including the
      unmeasured warm BoardMeta read) has its own snapshot (``open_hooks``); the measured op still
      starts from a reset.
  H5  Machine state recorded in the JSON before and after the run (cpu %, live python processes,
      method) plus ``machine_idle_asserted`` filled by the operator (--machine-idle-asserted).
  H6  ``--delete-nodes-types gate`` builds delete_nodes_by_session with EVERY node type of the
      frozen manifest's schema authority (okto_pulse.core.kg.schema_contract.NODE_TYPES, digest
      checked against the manifest); the default ``harness`` keeps ["Decision", "Entity"] and the
      certified digest. The gate form changes operation_set_sha256 by design.
  H7  Hook on storage_local._open_descriptor (ctypes CreateFileW, invisible to the os.open hook).
  H8  Hooks on BufferPool.read_fresh_page / _read_page / _invalidate, with ``read_view_drops``
      derived in the harness from _invalidate arguments (all / file / doom_pinned). No production
      counter is added by this revision.

Attach strategy: the gate's storage root is workspace/.mp7/<g|l>/sha256(run_id)[:24]
(mpulse7_acceptance_backends.py:212-227). RUN #3's run_id is not on record, so the copied board
is RELOCATED under the digest of this profile's own run_id. DatabaseIdentity carries no path
(database.py: database_uuid/page_size/partitions_per_table/created_at_wall/format) and the
Community binding's physical_path is relative, so relocation is identity-preserving.

Plan: one FIXTURE BASE per backend (setup, unmeasured, ~N*20 commits), then one fresh copy of the
base per (family, pass). Two passes per family: RAW (the operation's wall under --mode, no hooks,
no cProfile -- DIAGNOSTIC in mode=reopen, the gate's warm-loop shape only in mode=continuous) and
INSTRUMENTED (attribution only; its wall is never compared to RAW). Both published.

Guard (run4 terminal): refuses unless pid 33244 is dead AND <scratch>/run4.terminal asserts pid_gone=true.

Environment (fail-closed)
-------------------------
  * Every checkout named in the report must have an EMPTY ``git status --porcelain`` (a dedicated
    worktree of the SHA), and ``okto_grafx`` / ``okto_pulse.community`` / ``okto_pulse.core`` must
    import from those very trees (verified in-process; ``--grafx`` names the Grafx tree).
  * ``numpy`` must import in THIS interpreter, or the run is refused: the Grafx ``.venv`` (3.11,
    no numpy) cannot produce ``[accel]`` evidence. The interpreter that can, on this host, is
    ``C:\\Python313\\python.exe`` (numpy 2.5.1, ladybug 0.16.0); versions and origins of numpy and
    ladybug are recorded under ``environment``. Nothing is installed during a run.
  * ``--mode reopen`` (default) is DIAGNOSTIC (attach-fence workaround); ``--mode continuous`` is
    the gate's warm loop and needs an engine without the fence (A1 5002a77 or later).

Usage
-----
  set PYTHONDONTWRITEBYTECODE=1
  set PYTHONPATH=<Community>\\src;<Core>\\src;<Grafx worktree>\\src
  C:\\Python313\\python.exe -B profile_m7_families.py --backend grafx --grafx <Grafx worktree> ^
      --mode reopen --per-family 5 --out grafx-before.json          (baseline 6d9b7a1: reopen only)
  C:\\Python313\\python.exe -B profile_m7_families.py --backend grafx --grafx <A1 worktree> ^
      --mode continuous --per-family 5 --out grafx-after.json       (A1 5002a77+: the decisive one)
  C:\\Python313\\python.exe -B profile_m7_families.py --backend ladybug --grafx <Grafx worktree> ^
      --mode continuous --per-family 5 --out ladybug.json
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import functools
import hashlib
import importlib
import importlib.metadata
import inspect
import io
import json
import os
import platform
import pstats
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

SCRATCH = Path(
    r"C:\Users\jpamb\AppData\Local\Temp\claude\D--Projetos-Techridy-okto-grafx"
    r"\9699f9ef-9534-43db-9888-7031689e86f5\scratchpad"
).resolve()
FORENSIC_WORKSPACE = SCRATCH / "m7fail" / "workspace"
TERMINAL_MARKER = SCRATCH / "run4.terminal"
RUN4_PID = 33244
HARNESS_REVISION = "h1-h8"
CERTIFIED_SOURCE_SHA256 = (
    "0cb60d5e43e29ac82e9a41534668c2237b7be78534fca789b4f37c4601862591"
)
DELETE_NODES_TYPES_HARNESS = ("Decision", "Entity")
# The versioned harness names ITS OWN tree as the Community checkout (this file lives in tools/);
# --community overrides it, e.g. to measure against another worktree of the SHA you will cite.
COMMUNITY = Path(__file__).resolve().parents[1]
CORE = Path(r"D:\Projetos\Techridy\okto-pulse-core-mpulse6-logical-transfer-manifest")
GRAFX = Path(r"D:\Projetos\Techridy\okto_grafx")
PROFILE_RUN_ID = (
    "m7-profile-fase0a"  # the copied board is relocated under this run_id's digest
)
# Values the Community adapter's exact projection scope requires (read from the Core contract:
# relational_projection.RELATIONAL_PROJECTION_SYSTEM_ACTOR_PREFIX == "system:" and
# interfaces/graph_transaction.SOURCE_PROJECTION_REMOVED_REASON == "source_projection_removed").
# Spelled out here so the fixture plan -- and its digest -- do not depend on importing the trees.
PROJECTION_SYSTEM_AGENT = (
    "system:layer1_worker"  # the frozen create_node template's actor (system:*)
)
PROJECTION_REMOVED_REASON = "source_projection_removed"
BOARD_ID = "m-pulse-7-acceptance"
SEED = "x1"  # fixed: identical fixtures for both backends

FAMILIES = (
    "create_node",
    "create_edge",
    "update_node",
    "replace_node_payload",
    "mark_superseded",
    "increment_attestation",
    "replace_with_source_deleted_tombstone",
    "reconcile_spec_lineage_parent",
    "clear_spec_lineage_parent",
    "reconcile_projection_active_set",
    "delete_edges_by_session",
    "delete_nodes_by_session",
)
STORE_CAPABLE = {
    "create_node",
    "create_edge",
    "update_node",
    "mark_superseded",
    "increment_attestation",
    "delete_edges_by_session",
    "delete_nodes_by_session",
}
LINEAGE_RULE = "belongs_to/spec_to_refinement@trace-v1"


class ProfileFailure(RuntimeError):
    """Any condition that would make the measurement unrepresentative."""


def _explain(failure: BaseException) -> str:
    """Render the exception chain with any `details` mapping, so wrapped engine errors show."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = failure
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__}: {current}"
        details = getattr(current, "details", None)
        if details:
            text += f" details={details!r}"
        parts.append(text[:600])
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


# --- guards ---------------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return str(pid) in out


def _require_run4_terminal() -> dict[str, Any]:
    if _pid_alive(RUN4_PID):
        raise ProfileFailure(f"RUN #4 (pid {RUN4_PID}) is alive — refusing to profile.")
    if not TERMINAL_MARKER.exists():
        raise ProfileFailure(
            f"{TERMINAL_MARKER} is absent — no verified terminal state for RUN #4."
        )
    marker = json.loads(TERMINAL_MARKER.read_text(encoding="utf-8"))
    if marker.get("pid_gone") is not True or "verified_at_utc" not in marker:
        raise ProfileFailure(
            "terminal marker does not carry a positive proof (pid_gone=true, verified_at_utc)"
        )
    return marker


def _safe_rmtree(target: Path) -> None:
    resolved = target.resolve()
    if resolved.parent != SCRATCH or not resolved.name.startswith("m7profile-"):
        raise ProfileFailure(
            f"refusing to delete {resolved}: not a m7profile-* child of SCRATCH"
        )
    shutil.rmtree(resolved)


def _content_digest(root: Path) -> tuple[str, int, int]:
    """sha256 over (relative path, size, sha256(bytes)) of every file — content-authenticating."""
    outer = hashlib.sha256()
    count = total = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        inner = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                inner.update(chunk)
        size = path.stat().st_size
        outer.update(
            f"{path.relative_to(root).as_posix()}\0{size}\0{inner.hexdigest()}\n".encode()
        )
        count += 1
        total += size
    return outer.hexdigest(), count, total


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git_status(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=normal"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def _require_clean_checkout(name: str, repo: Path) -> dict[str, str]:
    """A number is attributable to a SHA only if the tree IS that SHA (rule cxxviii).

    Fase 0a run 1 recorded checkouts.grafx=6d9b7a1 while the imported tree carried an
    uncommitted A1 diff (the primary checkout was on perf/a-index-freshness). HEAD is not the
    tree. No bypass flag: measure on a clean worktree of the SHA you will cite.
    """
    dirty = _git_status(
        repo
    )  # nothing is tolerated: measure on a dedicated worktree whose status is empty
    if dirty:
        raise ProfileFailure(
            f"{name} checkout {repo} is NOT clean ({len(dirty)} entries, first: {dirty[:4]}); measure on a clean worktree of the SHA you will cite"
        )
    return {"head": _git_head(repo), "path": str(repo), "status": "clean"}


def _require_imported_from(module: str, repo: Path) -> str:
    """The tree the report names must be the tree this process imported (rule cxxviii)."""
    origin = Path(importlib.import_module(module).__file__).resolve()
    if repo.resolve() not in origin.parents:
        raise ProfileFailure(
            f"{module} was imported from {origin}, outside the checkout the report names ({repo}); fix PYTHONPATH/--grafx"
        )
    return str(origin)


def _dir_bytes(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total


# --- workspace preparation ------------------------------------------------------------------


def _letter(backend_name: str) -> str:
    return "l" if backend_name == "ladybug" else "g"


def _relocated_copy(label: str, backend_name: str, source_workspace: Path) -> Path:
    """Copy a workspace and relocate its single board root under PROFILE_RUN_ID's digest."""
    target = SCRATCH / f"m7profile-{label}"
    if target.exists():
        _safe_rmtree(target)
    workspace = target / "workspace"
    shutil.copytree(source_workspace, workspace)
    letter_dir = workspace / ".mp7" / _letter(backend_name)
    roots = (
        [p for p in letter_dir.iterdir() if p.is_dir()] if letter_dir.exists() else []
    )
    if len(roots) != 1:
        raise ProfileFailure(
            f"expected exactly one storage root under {letter_dir}, found {len(roots)}"
        )
    wanted = (
        letter_dir / hashlib.sha256(PROFILE_RUN_ID.encode("utf-8")).hexdigest()[:24]
    )
    if roots[0] != wanted:
        roots[0].rename(wanted)
    return workspace


def _active_root(workspace: Path, backend_name: str) -> dict[str, Any]:
    """BLOCKER 3: resolve the measured backend's board from its own binding, never rglob."""
    letter_dir = workspace / ".mp7" / _letter(backend_name)
    roots = [p for p in letter_dir.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise ProfileFailure(
            f"expected exactly one storage root under {letter_dir}, found {len(roots)}"
        )
    kg = roots[0] / "kg"
    binding_path = kg / "boards" / BOARD_ID / "graph_backend_binding.json"
    if not binding_path.exists():
        raise ProfileFailure(f"binding missing: {binding_path}")
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if binding.get("backend") != backend_name:
        raise ProfileFailure(
            f"binding backend {binding.get('backend')!r} != measured {backend_name!r}"
        )
    physical = kg / str(binding["physical_path"])
    if not physical.exists():
        raise ProfileFailure(f"physical_path does not exist: {physical}")
    if backend_name == "grafx":
        wal = physical / "wal"
        if not wal.is_dir():
            raise ProfileFailure(f"grafx wal dir missing: {wal}")
        measured = [wal]
    else:
        measured = [physical, Path(str(physical) + ".wal")]
    return {
        "kg": kg,
        "physical_path": physical,
        "wal_paths": measured,
        "binding": binding,
    }


# --- instrumentation --------------------------------------------------------------------------


class Hooks:
    OS_NAMES = (
        "stat",
        "lstat",
        "fstat",
        "scandir",
        "listdir",
        "fsync",
        "replace",
        "open",
        "close",
    )  # H2: +close
    ENGINE_TARGETS = (
        ("okto_grafx.engine.index_manager", "IndexStore", "advance_built_through"),
        ("okto_grafx.engine.buffer_pool", "BufferPool", "flush"),
        ("okto_grafx.engine.buffer_pool", "BufferPool", "_write_back"),
        ("okto_grafx.engine.database", "Database", "_catalog_snapshot"),
        ("okto_grafx.engine.wal_manager", "WalManager", "_refresh_tail"),
        ("okto_grafx.engine.wal_manager", "WalManager", "_discover"),
        ("okto_grafx.adapters.storage_local", "LocalStorageDevice", "list_files"),
        ("okto_grafx.adapters.storage_local", "LocalStorageDevice", "_walk"),
        ("okto_grafx.adapters.storage_local", "LocalStorageDevice", "_still_names"),
        ("okto_grafx.adapters.storage_local", "LocalStorageDevice", "write_page"),
        ("okto_grafx.engine.query_engine", "QueryEngine", "parse"),
        ("okto_grafx.engine.query_engine", "QueryEngine", "plan"),
        (
            "okto_pulse.community.adapters.graph_backend_binding",
            "CommunityGraphBackendBindingStore",
            "acquire_board_binding",
        ),
        (
            "okto_pulse.community.adapters.graph_backend_binding",
            "CommunityGraphBackendBindingStore",
            "inspect_board_binding",
        ),
        # H2 -- per-statement attribution (frente D, item M-2) and the OS-lock waits
        ("okto_grafx.engine.database", "Database", "_run_statement"),
        ("okto_grafx.engine.query_engine", "QueryEngine", "execute"),
        ("okto_grafx.engine.query_engine", "QueryEngine", "_planned_for"),
        ("okto_grafx.engine.query_engine", "QueryEngine", "planned"),
        (
            "okto_grafx.adapters.coordination_local",
            "LocalProcessCoordinator",
            "_take_file_lock",
        ),
        (
            "okto_grafx.adapters.coordination_local",
            "LocalProcessCoordinator",
            "_wait_for_os_lock",
        ),
        (
            "okto_grafx.adapters.coordination_local",
            "LocalProcessCoordinator",
            "_drop_lock",
        ),
        # H8 -- fresh vs cached page reads and read-view drops
        ("okto_grafx.engine.buffer_pool", "BufferPool", "read_fresh_page"),
        ("okto_grafx.engine.buffer_pool", "BufferPool", "_read_page"),
        ("okto_grafx.engine.buffer_pool", "BufferPool", "_invalidate"),
    )
    MODULE_FUNCTIONS = (
        ("okto_grafx.adapters.storage_local", "_windows_posix_replace"),
        (
            "okto_grafx.adapters.storage_local",
            "_open_descriptor",
        ),  # H7: ctypes CreateFileW, invisible to os.open
        ("okto_grafx.engine.public_views", "_query_parameters_snapshot"),  # H2
        ("okto_grafx.engine.public_views", "_query_result_view"),  # H2
    )
    WINDOWS_MODULE_FUNCTIONS = (
        ("msvcrt", "locking"),
    )  # H2: the OS lock behind LocalProcessCoordinator
    # H2: context managers cannot be timed by ``counted`` (the wrapper returns before the body runs
    # and would report ~0); they are deliberately NOT hooked. Attribute their cost by difference.
    EXCLUDED_CONTEXT_MANAGERS = (
        "TransactionManager.page_access_section",
        "LocalProcessCoordinator._section",
    )
    ATTRIBUTION_NOTE = "inclusive_ms is inclusive and nested: attribute by difference, never by summing nested keys"

    def __init__(self, backend_name: str) -> None:
        self.backend_name = backend_name
        self.calls: Counter[str] = Counter()
        self.nanos: Counter[str] = Counter()
        self.page_writes_by_file: Counter[str] = Counter()
        self.invalidations: Counter[str] = Counter()  # H8: all / file / doom_pinned
        self.unmeasurable: list[
            str
        ] = []  # H2: targets whose inclusive time the wrapper cannot see
        self._restore: list[tuple[Any, str, Any]] = []

    def install(self) -> None:
        for name in self.OS_NAMES:
            self._wrap(os, name, f"os.{name}")
        grafx = self.backend_name == "grafx"
        for module_name, class_name, method in self.ENGINE_TARGETS:
            try:
                owner = getattr(importlib.import_module(module_name), class_name)
                getattr(owner, method)
            except (ImportError, AttributeError) as failure:
                if module_name.startswith("okto_grafx.") and not grafx:
                    continue
                raise ProfileFailure(
                    f"hook target missing: {module_name}.{class_name}.{method}"
                ) from failure
            if method == "_write_back":
                self._wrap_write_back(owner)
            elif method == "_invalidate":
                self._wrap_invalidate(owner)
            else:
                self._wrap(owner, method, f"{class_name}.{method}")
        for module_name, function in self.MODULE_FUNCTIONS:
            try:
                module = importlib.import_module(module_name)
                getattr(module, function)
            except (ImportError, AttributeError) as failure:
                if not grafx:
                    continue
                raise ProfileFailure(
                    f"hook target missing: {module_name}.{function}"
                ) from failure
            self._wrap(module, function, function)
        if sys.platform == "win32":
            for module_name, function in self.WINDOWS_MODULE_FUNCTIONS:
                module = importlib.import_module(module_name)
                self._wrap(module, function, f"{module_name}.{function}")

    def _wrap(self, owner: Any, attribute: str, key: str) -> None:
        original = getattr(owner, attribute)
        if (
            inspect.isgeneratorfunction(original)
            or inspect.isasyncgenfunction(original)
            or inspect.iscoroutinefunction(original)
        ):
            # Calls are counted; the inclusive time would stop at generator/coroutine creation.
            self.unmeasurable.append(key)
        calls, nanos = self.calls, self.nanos

        @functools.wraps(original)
        def counted(*args: Any, **kwargs: Any) -> Any:
            calls[key] += 1
            started = time.perf_counter_ns()
            try:
                return original(*args, **kwargs)
            finally:
                nanos[key] += time.perf_counter_ns() - started

        self._restore.append((owner, attribute, original))
        setattr(owner, attribute, counted)

    def _wrap_write_back(self, owner: Any) -> None:
        original = owner._write_back
        calls, nanos, by_file = self.calls, self.nanos, self.page_writes_by_file

        @functools.wraps(original)
        def counted(self_: Any, file: str, page_index: Any, page: Any) -> Any:
            calls["BufferPool._write_back"] += 1
            by_file[str(file)] += 1
            started = time.perf_counter_ns()
            try:
                return original(self_, file, page_index, page)
            finally:
                nanos["BufferPool._write_back"] += time.perf_counter_ns() - started

        self._restore.append((owner, "_write_back", original))
        owner._write_back = counted

    def _wrap_invalidate(self, owner: Any) -> None:
        """H8: BufferPool._invalidate(file, *, doom_pinned) -- classify the read-view drop."""
        original = owner._invalidate
        calls, nanos, drops = self.calls, self.nanos, self.invalidations

        @functools.wraps(original)
        def counted(self_: Any, file: Any, *args: Any, **kwargs: Any) -> Any:
            calls["BufferPool._invalidate"] += 1
            drops["all" if file is None else "file"] += 1
            if kwargs.get("doom_pinned") or (args and args[0]):
                drops["doom_pinned"] += 1
            started = time.perf_counter_ns()
            try:
                return original(self_, file, *args, **kwargs)
            finally:
                nanos["BufferPool._invalidate"] += time.perf_counter_ns() - started

        self._restore.append((owner, "_invalidate", original))
        owner._invalidate = counted

    def uninstall(self) -> None:
        for owner, attribute, original in reversed(self._restore):
            setattr(owner, attribute, original)
        self._restore.clear()

    def reset(self) -> None:
        self.calls.clear()
        self.nanos.clear()
        self.page_writes_by_file.clear()
        self.invalidations.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "inclusive_ms": {k: v / 1e6 for k, v in self.nanos.items()},
            "page_writes_total": sum(self.page_writes_by_file.values()),
            "page_writes_index_files": sum(
                n
                for f, n in self.page_writes_by_file.items()
                if "index/" in f.replace("\\", "/")
            ),
            "page_writes_top_files": self.page_writes_by_file.most_common(12),
            "read_view_drops": {
                "all": self.invalidations["all"],
                "file": self.invalidations["file"],
                "doom_pinned": self.invalidations["doom_pinned"],
            },
            "unmeasurable_targets": list(self.unmeasurable),
            "attribution_note": self.ATTRIBUTION_NOTE,
        }


class PhaseTimers:
    """H3 -- three timers per operation (begin / each execute / commit) and nothing else.

    Installed on the backend INSTANCE (never on classes, never on ``os``): ``graph_transaction.begin``
    is replaced by a wrapper that times the begin and, on the scope it returns, times every
    ``execute`` and the final ``commit``/``rollback``. No cProfile, no OS hooks. If the backend or
    the scope refuses instance attributes the timers are reported as unavailable (with the reason)
    and the pass still runs; the pass is published beside RAW, never in its place.
    """

    def __init__(self) -> None:
        self.available = True
        self.reason: str | None = None
        self._restore: list[tuple[Any, str, Any]] = []
        self.begin_ns = 0
        self.begins = 0
        self.execute_ns: list[int] = []
        self.commit_ns = 0
        self.rollback_ns = 0

    def reset(self) -> None:
        self.begin_ns = 0
        self.begins = 0
        self.execute_ns = []
        self.commit_ns = 0
        self.rollback_ns = 0

    def _unavailable(self, failure: BaseException) -> None:
        self.available = False
        self.reason = f"{type(failure).__name__}: {failure}"

    def attach(self, backend: Any) -> None:
        timers = self
        try:
            transaction = backend.graph_transaction
            original_begin = transaction.begin

            async def begin(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter_ns()
                scope = original_begin(*args, **kwargs)
                if inspect.isawaitable(scope):
                    scope = await scope
                timers.begin_ns += time.perf_counter_ns() - started
                timers.begins += 1
                timers._wrap_scope(scope)
                return scope

            transaction.begin = begin
            self._restore.append((transaction, "begin", original_begin))
        except Exception as failure:  # noqa: BLE001 -- timers are optional; the pass never fails for them
            self._unavailable(failure)

    def _record(self, name: str, nanos: int) -> None:
        if name == "commit":
            self.commit_ns += nanos
        else:
            self.rollback_ns += nanos

    def _wrap_scope(self, scope: Any) -> None:
        timers = self
        try:
            original_execute = scope.execute

            def execute(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter_ns()
                try:
                    return original_execute(*args, **kwargs)
                finally:
                    timers.execute_ns.append(time.perf_counter_ns() - started)

            scope.execute = execute
            for name in ("commit", "rollback"):
                original = getattr(scope, name)

                def timed(
                    *args: Any,
                    _original: Any = original,
                    _name: str = name,
                    **kwargs: Any,
                ) -> Any:
                    started = time.perf_counter_ns()
                    result = _original(*args, **kwargs)
                    if inspect.isawaitable(result):

                        async def awaiting() -> Any:
                            try:
                                return await result
                            finally:
                                timers._record(_name, time.perf_counter_ns() - started)

                        return awaiting()
                    timers._record(_name, time.perf_counter_ns() - started)
                    return result

                setattr(scope, name, timed)
        except Exception as failure:  # noqa: BLE001
            self._unavailable(failure)

    def detach(self) -> None:
        for owner, attribute, original in reversed(self._restore):
            setattr(owner, attribute, original)
        self._restore.clear()

    def snapshot(self) -> dict[str, Any]:
        execute_ms = [n / 1e6 for n in self.execute_ns]
        return {
            "available": self.available,
            "reason": self.reason,
            "begins": self.begins,
            "begin_ms": self.begin_ns / 1e6,
            "execute_count": len(execute_ms),
            "execute_total_ms": sum(execute_ms),
            "execute_ms": execute_ms,
            "commit_ms": self.commit_ns / 1e6,
            "rollback_ms": self.rollback_ns / 1e6,
        }


def harness_revision() -> dict[str, Any]:
    """Identity of THIS harness file, recorded in every report (acceptance criterion 5)."""
    here = Path(__file__).resolve()
    return {
        "name": HARNESS_REVISION,
        "file": str(here),
        "sha256": hashlib.sha256(here.read_bytes()).hexdigest(),
        "certified_source_sha256": CERTIFIED_SOURCE_SHA256,
        "raw_pass": (
            "logically identical to the certified source (no hooks, no cProfile, same open/close/timing "
            "sequence); RAW medians are comparable across harness revisions when operation_set_sha256 agrees"
        ),
        "instrumented_comparability": (
            "instrumented and phases passes are comparable ONLY between reports with the same "
            "harness_revision.sha256 (the hook set and the timers changed in h1-h8)"
        ),
    }


def _machine_state() -> dict[str, Any]:
    """H5: cpu busy % and live python processes, sampled before and after a run."""
    state: dict[str, Any] = {
        "sampled_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    try:
        import psutil  # noqa: PLC0415

        state["cpu_percent"] = psutil.cpu_percent(interval=2.0)
        state["cpu_count"] = psutil.cpu_count()
        state["python_processes"] = sum(
            1
            for process in psutil.process_iter(["name"])
            if str(process.info.get("name") or "").lower().startswith("python")
        )
        state["method"] = "psutil"
        return state
    except ImportError:
        pass
    except Exception as failure:  # noqa: BLE001 -- recorded, never fatal
        state["error"] = repr(failure)
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["typeperf", "\\Processor(_Total)\\% Processor Time", "-sc", "2"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            ).stdout
            rows = [
                line
                for line in out.splitlines()
                if line.startswith('"') and "Processor" not in line
            ]
            state["cpu_percent"] = (
                float(rows[-1].split(",")[-1].strip('"')) if rows else None
            )
            tasks = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            state["python_processes"] = sum(
                1 for line in tasks.splitlines() if line.lower().startswith("python")
            )
            state["method"] = "typeperf"
            return state
        except Exception as failure:  # noqa: BLE001
            state["error"] = repr(failure)
    state.setdefault("cpu_percent", None)
    state.setdefault("python_processes", None)
    state["method"] = "unavailable"
    return state


# --- gate plumbing ----------------------------------------------------------------------------


def _load_gate_modules() -> tuple[Any, Any]:
    sys.path.insert(0, str(COMMUNITY / "tools"))
    sys.path.insert(0, str(COMMUNITY / "tests"))
    return importlib.import_module("run_mpulse7_acceptance"), importlib.import_module(
        "mpulse7_acceptance_backends"
    )


def _templates(runner: Any) -> dict[str, dict[str, Any]]:
    """First frozen operation of each family = the payload SHAPE we substitute identities into."""
    inputs = runner.verify_frozen_inputs(
        manifest_path=runner.MANIFEST_PATH, pulse_corpus_path=None, certification=False
    )
    operations = list(inputs.operations)
    if len(operations) != 10_000:
        raise ProfileFailure(f"expected 10000 frozen operations, got {len(operations)}")
    found: dict[str, dict[str, Any]] = {}
    for op in operations:
        fam = str(op["family"])
        if fam in FAMILIES and fam not in found:
            found[fam] = json.loads(json.dumps(op))
    missing = [f for f in FAMILIES if f not in found]
    if missing:
        raise ProfileFailure(f"no template in the manifest for: {missing}")
    return found


def _gate_node_types(runner: Any) -> list[str]:
    """H6: every node type of the frozen manifest's schema authority, digest-checked (fail-closed)."""
    from okto_pulse.core.kg.schema_contract import NODE_TYPES  # noqa: PLC0415 -- the manifest's declared authority

    types = list(NODE_TYPES)
    manifest = json.loads(Path(runner.MANIFEST_PATH).read_text(encoding="utf-8"))
    authority = manifest["trace"]["schema_authority"]
    digest = runner.canonical_sha256(types)
    if (
        len(types) != int(authority["node_type_count"])
        or digest != authority["node_types_sha256"]
    ):
        raise ProfileFailure(
            f"NODE_TYPES ({len(types)}, {digest[:12]}) do not match the manifest's schema authority "
            f"({authority['node_type_count']}, {str(authority['node_types_sha256'])[:12]}); refusing the gate form"
        )
    return types


def _plan_digest(
    plan: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
    families: list[str],
    path: str,
) -> str:
    """operation_set_sha256: LOGICAL operation set (family, method, path, payload, postcondition)."""
    return hashlib.sha256(
        json.dumps(
            [
                {
                    "family": op["family"],
                    "method": op["method"],
                    "path": path,
                    "payload": op["payload"],
                    "postcondition": post,
                }
                for f in families
                for op, post in plan[f]
            ],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


async def _open_backend(
    runner: Any, backends: Any, backend_name: str, workspace: Path
) -> tuple[Any, Any]:
    factory = (
        backends.ladybug_factory
        if backend_name == "ladybug"
        else backends.grafx_factory
    )
    context = runner.GateBackendContext(
        backend=backend_name,
        board_id=BOARD_ID,
        workspace=str(workspace),
        run_id=PROFILE_RUN_ID,
        certification_process_authority_sha256=None,
    )
    return await factory(context), context


async def _read_rows(
    backend: Any, statement: str, params: dict[str, Any]
) -> list[list[Any]]:
    scope = await backend.graph_transaction.begin(BOARD_ID)
    try:
        result = scope.execute(statement, params)
        rows = getattr(result, "rows", None)
        if rows is None:
            raise ProfileFailure("read returned a result without materialized rows")
        return [list(r) for r in rows]
    finally:
        await scope.rollback()


def _op(
    family: str, payload: dict[str, Any], path: str, sequence: int
) -> dict[str, Any]:
    if path == "store":
        if family not in STORE_CAPABLE:
            raise ProfileFailure(f"{family} has no SemanticGraphStore path")
        method = f"SemanticGraphStore.{family}"
    elif family == "replace_with_source_deleted_tombstone":
        method = f"GraphTransactionScopeExtension.{family}"
    else:
        method = f"GraphTransactionScope.{family}"
    # run_mpulse7_acceptance.py reads family/method/payload/sequence, and operation_id on its
    # error-reporting path (:1943); the support module formats ids as m-pulse-7-<seq:05d>.
    return {
        "sequence": sequence,
        "operation_id": f"m7-profile-{sequence:05d}",
        "family": family,
        "method": method,
        "payload": payload,
    }


# --- fixtures: setup ops and measured ops with postconditions ------------------------------------


class Fixtures:
    """Deterministic fixture plan shared by both backends (BLOCKER 2)."""

    def __init__(
        self,
        templates: dict[str, dict[str, Any]],
        per_family: int,
        path: str,
        delete_node_types: tuple[str, ...] = DELETE_NODES_TYPES_HARNESS,
    ) -> None:
        self.t = templates
        self.n = per_family
        self.path = path
        self.delete_node_types = tuple(
            delete_node_types
        )  # H6: harness form or the manifest's gate form
        self.session = f"prof-{SEED}"
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    # -- payload builders from templates --
    def _node(
        self, node_type: str, node_id: str, session: str, content: str
    ) -> dict[str, Any]:
        attrs = dict(self.t["create_node"]["payload"]["attrs"])
        # The manifest template is the STORE-path shape, whose attrs may carry the identity
        # properties; the SCOPE path forbids them inside attrs (grafx adapter: "forbidden=
        # ['source_session_id']") and takes source_session_id as a keyword instead.
        attrs.pop("source_session_id", None)
        attrs.pop("id", None)
        attrs["content"] = content
        return {
            "node_type": node_type,
            "node_id": node_id,
            "attrs": attrs,
            "source_session_id": session,
        }

    def _edge(
        self, from_id: str, to_id: str, session: str, marker: str
    ) -> dict[str, Any]:
        p = self.t["create_edge"]["payload"]
        attrs = dict(p["attrs"])
        attrs["created_by_session_id"] = session
        attrs["fallback_reason"] = marker
        return {
            "edge_type": "belongs_to",
            "from_type": "Decision",
            "to_type": "Entity",
            "from_id": from_id,
            "to_id": to_id,
            "attrs": attrs,
        }

    def _lineage_attrs(self) -> dict[str, Any]:
        attrs = dict(self.t["reconcile_spec_lineage_parent"]["payload"]["attrs"])
        attrs["created_by_session_id"] = self.session
        return attrs

    def ids(self, kind: str, i: int, suffix: str = "") -> str:
        return f"prof-{SEED}-{kind}-{i:03d}{suffix}"

    # -- setup: everything the measured operations need, created under private sessions --
    def setup_ops(self) -> list[dict[str, Any]]:
        s, ops = self.session, []

        def node(t: str, nid: str, session: str = s) -> None:
            ops.append(
                _op(
                    "create_node",
                    self._node(t, nid, session, f"setup-{nid}"),
                    "scope",
                    self._next(),
                )
            )

        for i in range(self.n):
            node("Decision", self.ids("upd", i))
            node("Criterion", self.ids("rep", i))
            node("Decision", self.ids("sup", i))
            node("Decision", self.ids("supby", i))
            node("Learning", self.ids("att", i))
            node("Constraint", self.ids("tomb", i))
            node("Entity", self.ids("lin", i, "-src"))
            node("Entity", self.ids("lin", i, "-tgt"))
            node("Entity", self.ids("clr", i, "-src"))
            node("Entity", self.ids("clr", i, "-tgt"))
            node("Decision", self.ids("edge", i, "-a"))
            node("Entity", self.ids("edge", i, "-b"))
            node("Decision", self.ids("de", i, "-a"))
            node("Entity", self.ids("de", i, "-b"))
            for k in range(3):
                ops.append(
                    _op(
                        "create_edge",
                        self._edge(
                            self.ids("de", i, "-a"),
                            self.ids("de", i, "-b"),
                            f"{s}-edges-{i:03d}",
                            f"de-{i:03d}-{k}",
                        ),
                        "scope",
                        self._next(),
                    )
                )
            node("Decision", self.ids("dn", i, "-a"), f"{s}-nodes-{i:03d}")
            node("Entity", self.ids("dn", i, "-b"), f"{s}-nodes-{i:03d}")
            # lineage parent to be cleared by the measured clear_* op
            ops.append(
                _op(
                    "reconcile_spec_lineage_parent",
                    {
                        "source_id": self.ids("clr", i, "-src"),
                        "target_id": self.ids("clr", i, "-tgt"),
                        "attrs": self._lineage_attrs(),
                    },
                    "scope",
                    self._next(),
                )
            )
            # Projection (refinement/rdl) fixture, in the EXACT scope the Community adapter
            # enforces (grafx_graph_transaction.py:2660-2679 and :2472-2533; Core
            # relational_projection.py _RDL_REF_PATTERN): the member's source_artifact_ref must
            # parse as refinement:<owner_id>:rdl:<ledger>:decision with the intent's owner_id,
            # its created_by_agent must start with "system:", and it is OWNED either through a
            # belongs_to edge to the owner Entity or because it already carries the projection's
            # own removal reason. The fixture takes the second road: the member is created
            # already tombstoned, so the measured reconcile (empty -> 1 member) RESTORES it --
            # a write by construction with a canonical read-back (revocation_reason -> "").
            # Pinned by tests/test_grafx_projection_active_set.py:517-534 (restore) and :698-731
            # (foreign owner refused / unowned member missing). fase 0a run 1 (Codex smoke) failed
            # here with "An active member is outside the exact projection scope" because the
            # member carried the frozen create_node template's ref (another owner).
            owner, member = self._projection_identities(i)
            owner_attrs = self._node("Entity", owner["id"], s, f"proj-owner-{i:03d}")
            owner_attrs["attrs"]["source_artifact_ref"] = owner["ref"]
            ops.append(_op("create_node", owner_attrs, "scope", self._next()))
            member_attrs = self._node(
                "Decision", member["id"], s, f"proj-member-{i:03d}"
            )
            member_attrs["attrs"].update(
                {
                    "source_artifact_ref": member["ref"],
                    "created_by_agent": PROJECTION_SYSTEM_AGENT,
                    "revocation_reason": PROJECTION_REMOVED_REASON,
                }
            )
            ops.append(_op("create_node", member_attrs, "scope", self._next()))
        return ops

    def _projection_identities(self, i: int) -> tuple[dict[str, str], dict[str, str]]:
        """Owner Entity and one Decision member of the exact refinement/rdl projection scope."""
        owner_id = f"prof-owner-{SEED}-{i:03d}"
        return (
            {
                "id": self.ids("proj", i, "-owner"),
                "owner_id": owner_id,
                "ref": f"refinement:{owner_id}",
            },
            {
                "id": self.ids("proj", i),
                "ref": f"refinement:{owner_id}:rdl:prof-ledger-{i:03d}:decision",
            },
        )

    # -- measured: (operation, postcondition) per family, i = sample index --
    def measured(self, family: str, i: int) -> tuple[dict[str, Any], dict[str, Any]]:
        p, s = self.path, self.session
        if family == "create_node":
            nid = self.ids("new", i)
            return _op(
                family, self._node("Decision", nid, s, f"new-{i}"), p, self._next()
            ), {"kind": "node_exists", "type": "Decision", "id": nid}
        if family == "create_edge":
            marker = f"edge-{i:03d}"
            return _op(
                family,
                self._edge(
                    self.ids("edge", i, "-a"), self.ids("edge", i, "-b"), s, marker
                ),
                p,
                self._next(),
            ), {"kind": "edge_marker_exists", "marker": marker}
        if family == "update_node":
            attrs = dict(self.t[family]["payload"]["attrs"])
            attrs["content"] = f"upd-{i:03d}"
            return _op(
                family,
                {
                    "node_type": "Decision",
                    "node_id": self.ids("upd", i),
                    "attrs": attrs,
                },
                p,
                self._next(),
            ), {
                "kind": "node_attr",
                "type": "Decision",
                "id": self.ids("upd", i),
                "attr": "content",
                "expected": f"upd-{i:03d}",
            }
        if family == "replace_node_payload":
            attrs = dict(self.t[family]["payload"]["attrs"])
            attrs["content"] = f"rep-{i:03d}"
            return _op(
                family,
                {
                    "node_type": "Criterion",
                    "node_id": self.ids("rep", i),
                    "attrs": attrs,
                    "source_session_id": s,
                },
                p,
                self._next(),
            ), {
                "kind": "node_attr",
                "type": "Criterion",
                "id": self.ids("rep", i),
                "attr": "content",
                "expected": f"rep-{i:03d}",
            }
        if family == "mark_superseded":
            tpl = self.t[family]["payload"]
            reason = f"sup-{i:03d}"
            return _op(
                family,
                {
                    "node_type": "Decision",
                    "node_id": self.ids("sup", i),
                    "superseded_by": self.ids("supby", i),
                    "superseded_at": tpl["superseded_at"],
                    "revocation_reason": reason,
                },
                p,
                self._next(),
            ), {
                "kind": "node_attr",
                "type": "Decision",
                "id": self.ids("sup", i),
                "attr": "revocation_reason",
                "expected": reason,
            }
        if family == "increment_attestation":
            tpl = self.t[family]["payload"]
            return _op(
                family,
                {
                    "node_type": "Learning",
                    "node_id": self.ids("att", i),
                    "attested_at": tpl["attested_at"],
                },
                p,
                self._next(),
            ), {
                "kind": "attestation_plus_one",
                "type": "Learning",
                "id": self.ids("att", i),
            }
        if family == "replace_with_source_deleted_tombstone":
            tpl = dict(self.t[family]["payload"])
            tpl.update({"node_type": "Constraint", "node_id": self.ids("tomb", i)})
            return _op(family, tpl, p, self._next()), {
                "kind": "node_attrs",
                "type": "Constraint",
                "id": self.ids("tomb", i),
                "expected": {
                    "maturity_status": tpl["maturity_status"],
                    "revocation_reason": tpl["revocation_reason"],
                },
            }
        if family == "reconcile_spec_lineage_parent":
            return _op(
                family,
                {
                    "source_id": self.ids("lin", i, "-src"),
                    "target_id": self.ids("lin", i, "-tgt"),
                    "attrs": self._lineage_attrs(),
                },
                p,
                self._next(),
            ), {
                "kind": "lineage_present",
                "src": self.ids("lin", i, "-src"),
                "tgt": self.ids("lin", i, "-tgt"),
                "expected": True,
            }
        if family == "clear_spec_lineage_parent":
            return _op(
                family, {"source_id": self.ids("clr", i, "-src")}, p, self._next()
            ), {
                "kind": "lineage_present",
                "src": self.ids("clr", i, "-src"),
                "tgt": self.ids("clr", i, "-tgt"),
                "expected": False,
            }
        if family == "reconcile_projection_active_set":
            owner, member = self._projection_identities(i)
            proj = dict(
                self.t[family]["payload"]
            )  # frozen shape: refinement / rdl, no edges
            proj.update(
                {
                    "owner_type": "refinement",
                    "namespace": "rdl",
                    "owner_id": owner["owner_id"],
                    "owner_node_id": owner["id"],
                    "active_nodes": [
                        {
                            "node_type": "Decision",
                            "node_id": member["id"],
                            "source_artifact_ref": member["ref"],
                        }
                    ],
                    "active_edges": [],
                }
            )
            # empty -> 1 member: the tombstoned member returns to the active set, so the adapter
            # RESTORES it; the canonical public read is its revocation_reason (test :517-534).
            return _op(family, proj, p, self._next()), {
                "kind": "node_attr",
                "type": "Decision",
                "id": member["id"],
                "attr": "revocation_reason",
                "expected": "",
                "before_expected": PROJECTION_REMOVED_REASON,
            }
        if family == "delete_edges_by_session":
            return _op(family, {"session_id": f"{s}-edges-{i:03d}"}, p, self._next()), {
                "kind": "edges_in_session",
                "session": f"{s}-edges-{i:03d}",
                "expected": 0,
            }
        if family == "delete_nodes_by_session":
            return _op(
                family,
                {
                    "session_id": f"{s}-nodes-{i:03d}",
                    "node_types": list(self.delete_node_types),
                },
                p,
                self._next(),
            ), {
                "kind": "nodes_in_session",
                "session": f"{s}-nodes-{i:03d}",
                "expected": 0,
            }
        raise ProfileFailure(f"unknown family {family}")


async def _check(
    backend: Any, post: dict[str, Any], before: dict[str, Any] | None
) -> None:
    k = post["kind"]
    if k == "node_exists":
        rows = await _read_rows(
            backend,
            f"MATCH (n:{post['type']}) WHERE n.id = $id RETURN n.id",
            {"id": post["id"]},
        )
        ok = len(rows) == 1
    elif k == "node_attr":
        rows = await _read_rows(
            backend,
            f"MATCH (n:{post['type']}) WHERE n.id = $id RETURN n.{post['attr']}",
            {"id": post["id"]},
        )
        ok = len(rows) == 1 and rows[0][0] == post["expected"]
    elif k == "node_attrs":
        names = sorted(post["expected"])
        projection = ", ".join(f"n.{name}" for name in names)
        rows = await _read_rows(
            backend,
            f"MATCH (n:{post['type']}) WHERE n.id = $id RETURN {projection}",
            {"id": post["id"]},
        )
        ok = len(rows) == 1 and all(
            rows[0][j] == post["expected"][name] for j, name in enumerate(names)
        )
    elif k == "attestation_plus_one":
        rows = await _read_rows(
            backend,
            f"MATCH (n:{post['type']}) WHERE n.id = $id RETURN n.attestation_count",
            {"id": post["id"]},
        )
        ok = (
            len(rows) == 1
            and before is not None
            and rows[0][0] == (before.get("attestation_count") or 0) + 1
        )
    elif k == "edge_marker_exists":
        rows = await _read_rows(
            backend,
            "MATCH (a:Decision)-[r:belongs_to]->(b:Entity) WHERE r.fallback_reason = $m RETURN r.fallback_reason",
            {"m": post["marker"]},
        )
        ok = len(rows) == 1
    elif k == "lineage_present":
        rows = await _read_rows(
            backend,
            "MATCH (a:Entity)-[r:belongs_to]->(b:Entity) WHERE a.id = $s AND b.id = $t AND r.rule_id = $rule RETURN r.rule_id",
            {"s": post["src"], "t": post["tgt"], "rule": LINEAGE_RULE},
        )
        ok = (len(rows) == 1) == post["expected"]
    elif k == "edges_in_session":
        rows = await _read_rows(
            backend,
            "MATCH (a:Decision)-[r:belongs_to]->(b:Entity) WHERE r.created_by_session_id = $s RETURN r.created_at",
            {"s": post["session"]},
        )
        ok = len(rows) == post["expected"]
    elif k == "nodes_in_session":
        rows = await _read_rows(
            backend,
            "MATCH (n:Decision) WHERE n.source_session_id = $s RETURN n.id",
            {"s": post["session"]},
        )
        rows += await _read_rows(
            backend,
            "MATCH (n:Entity) WHERE n.source_session_id = $s RETURN n.id",
            {"s": post["session"]},
        )
        ok = len(rows) == post["expected"]
    elif k == "structural_write_variant":
        return  # labelled, not read back (see docstring)
    else:
        raise ProfileFailure(f"unknown postcondition {k}")
    if not ok:
        raise ProfileFailure(f"postcondition failed: {post}")


async def _pre(backend: Any, post: dict[str, Any]) -> dict[str, Any] | None:
    if post["kind"] == "attestation_plus_one":
        rows = await _read_rows(
            backend,
            f"MATCH (n:{post['type']}) WHERE n.id = $id RETURN n.attestation_count",
            {"id": post["id"]},
        )
        if len(rows) != 1:
            raise ProfileFailure(f"attestation target missing: {post['id']}")
        return {"attestation_count": rows[0][0]}
    if post["kind"] == "node_attr" and "before_expected" in post:
        # The postcondition only discriminates if the value really was different BEFORE the
        # operation (a restore that finds nothing to restore would pass a bare read-back).
        rows = await _read_rows(
            backend,
            f"MATCH (n:{post['type']}) WHERE n.id = $id RETURN n.{post['attr']}",
            {"id": post["id"]},
        )
        if len(rows) != 1:
            raise ProfileFailure(f"target missing before the operation: {post['id']}")
        if (rows[0][0] or "") != post["before_expected"]:
            raise ProfileFailure(
                f"precondition failed: {post['type']} {post['id']}.{post['attr']} = {rows[0][0]!r}, expected {post['before_expected']!r} before the operation"
            )
        return {post["attr"]: rows[0][0]}
    return None


# --- runs -------------------------------------------------------------------------------------


async def _build_base(
    runner: Any, backends: Any, backend_name: str, fixtures: Fixtures
) -> Path:
    """Setup once per backend: relocate the forensic board and apply the fixture plan (unmeasured)."""
    workspace = _relocated_copy(
        f"{backend_name}-base", backend_name, FORENSIC_WORKSPACE
    )
    _active_root(
        workspace, backend_name
    )  # proves the relocation is well-formed before opening
    ops = fixtures.setup_ops()
    started = time.perf_counter_ns()
    # ATTACH-FENCE WORKAROUND (reported to Codex for A1): in a process that attached to a board
    # made by another process, the first commit leaves every OTHER table's index refused
    # ("covers position X, before the snapshot at Y") until the backend is reopened. Reopen
    # before every operation; this is unmeasured setup, so only elapsed time is affected.
    for k, op in enumerate(ops):
        backend, context = await _open_backend(
            runner, backends, backend_name, workspace
        )
        try:
            await runner._execute_operation(backend, context, op)
        except Exception as failure:  # noqa: BLE001
            raise ProfileFailure(
                f"{backend_name} setup op#{k} {op['family']} failed: {_explain(failure)}"
            ) from failure
        finally:
            await backend.close()
    print(
        f"{backend_name}: fixture base built — {len(ops)} setup commits (one reopen each) in {(time.perf_counter_ns() - started) / 1e9:.1f}s",
        flush=True,
    )
    return workspace


async def _run_family(
    runner: Any,
    backends: Any,
    backend_name: str,
    base: Path,
    family: str,
    plan: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    path: str,
    mode: str,
    instrumented: bool,
    top: int,
    phases: bool = False,
) -> dict[str, Any]:
    """Run one family's pre-built plan on a fresh copy of the base, under the selected mode.

    mode=reopen     ATTACH-FENCE WORKAROUND (grafx 6d9b7a1): the backend is closed and reopened
                    before every sample AND before its read-back (the read-back may touch a table
                    the op did not write; fase 0a run 1 aborted there). Diagnostic numbers.
    mode=continuous one open per family pass, samples back-to-back on the same handle, read-back
                    on the same handle -- the gate's warm loop shape. An engine that still has the
                    attach fence refuses this honestly (the profile fails; nothing is relabelled).
    The open is timed separately (open_ms) and a warm read of BoardMeta is issued unmeasured so
    an operation never pays cold-open effects the gate's warm loop never paid.

    Passes: RAW (instrumented=False, phases=False -- unchanged since the certified source),
    INSTRUMENTED (hooks installed before the open [H4] + cProfile cumulative and tottime [H1]) and
    PHASES (three instance timers per op [H3]; no hooks, no cProfile).
    """
    kind = "instr" if instrumented else ("phases" if phases else "raw")
    root_workspace = _relocated_copy(
        f"{backend_name}-{family}-{kind}", backend_name, base
    )
    root = root_workspace.parent
    active = _active_root(root_workspace, backend_name)
    hooks = Hooks(backend_name) if instrumented else None
    timers = PhaseTimers() if phases else None
    samples: list[dict[str, Any]] = []
    reopen = mode == "reopen"
    backend: Any = None
    context: Any = None
    is_open = False
    last_open_hooks: dict[str, Any] | None = None

    async def _open() -> float:
        nonlocal backend, context, is_open, last_open_hooks
        if hooks:
            hooks.reset()  # H4: the open (and its warm read) gets its own snapshot
        if timers:
            timers.detach()
        opened = time.perf_counter_ns()
        backend, context = await _open_backend(
            runner, backends, backend_name, root_workspace
        )
        is_open = True
        open_ms = (time.perf_counter_ns() - opened) / 1e6
        await _read_rows(
            backend, "MATCH (m:BoardMeta) RETURN m.board_id", {}
        )  # warm, unmeasured
        if hooks:
            last_open_hooks = hooks.snapshot()
        if timers:
            timers.attach(backend)
        return open_ms

    async def _close() -> None:
        nonlocal is_open
        if is_open:
            is_open = False
            if timers:
                timers.detach()
            await backend.close()

    try:
        if hooks:
            hooks.install()  # H4: before the open; stays installed for the whole pass
        pass_open_ms = 0.0 if reopen else await _open()
        pass_open_hooks = last_open_hooks
        for i, (op, post) in enumerate(plan):
            open_ms = await _open() if reopen else (pass_open_ms if i == 0 else 0.0)
            open_hooks = (
                last_open_hooks if reopen else (pass_open_hooks if i == 0 else None)
            )
            try:
                if post.get("type") and post.get("id"):
                    await _read_rows(
                        backend,
                        f"MATCH (n:{post['type']}) WHERE n.id = $id RETURN n.id",
                        {"id": post["id"]},
                    )
                before = await _pre(backend, post)
                wal_before = _dir_bytes(active["wal_paths"])
                if hooks:
                    hooks.reset()
                if timers:
                    timers.reset()
                profiler = cProfile.Profile() if instrumented else None
                started = time.perf_counter_ns()
                if profiler:
                    profiler.enable()
                try:
                    await runner._execute_operation(backend, context, op)
                except Exception as failure:  # noqa: BLE001 -- BLOCKER 1/H1: any error fails the profile
                    raise ProfileFailure(
                        f"{backend_name}/{family} sample#{i} failed: {_explain(failure)}"
                    ) from failure
                finally:
                    if profiler:
                        profiler.disable()
                wall_ms = (time.perf_counter_ns() - started) / 1e6
                hook_snapshot = hooks.snapshot() if hooks else None
                phase_snapshot = timers.snapshot() if timers else None
                # Storage delta BEFORE any close: close may checkpoint and change the WAL footprint.
                storage_delta = _dir_bytes(active["wal_paths"]) - wal_before
                if reopen:
                    # Second workaround site (fase 0a run 1 aborted here at family 8): the read-back
                    # may touch a table the op did NOT write (reconcile_spec_lineage_parent reads
                    # Entity after writing the lineage edge) and the fence refuses it in the session
                    # that committed. Reopening also proves the effect on the DEVICE, not in cache.
                    await _close()
                    await _open()
                await _check(backend, post, before)
                sample: dict[str, Any] = {
                    "index": i,
                    "wall_ms": wall_ms,
                    "open_ms": open_ms,
                    # Ladybug's measured paths are graph.lbug (+ .wal): storage, not WAL alone.
                    "backend_storage_bytes_delta": storage_delta,
                    "postcondition": post["kind"],
                }
                if backend_name == "grafx":
                    sample["grafx_wal_bytes_delta"] = (
                        storage_delta  # measured paths == wal/ dir
                    )
                if hook_snapshot is not None and profiler is not None:
                    stream = io.StringIO()
                    pstats.Stats(profiler, stream=stream).sort_stats(
                        "cumulative"
                    ).print_stats(top)
                    sample["hooks"] = hook_snapshot
                    sample["cprofile_top"] = stream.getvalue().splitlines()[: top + 8]
                    stream_tottime = io.StringIO()  # H1
                    pstats.Stats(profiler, stream=stream_tottime).sort_stats(
                        "tottime"
                    ).print_stats(top)
                    sample["cprofile_top_tottime"] = (
                        stream_tottime.getvalue().splitlines()[: top + 8]
                    )
                    if open_hooks is not None:
                        sample["open_hooks"] = open_hooks  # H4
                if phase_snapshot is not None:
                    sample["phases"] = phase_snapshot  # H3
                samples.append(sample)
            finally:
                if reopen:
                    await _close()
        await _close()
    finally:
        if hooks:
            hooks.uninstall()
        await _close()
        _safe_rmtree(root)  # BLOCKER 4
    walls = [s["wall_ms"] for s in samples]
    summary: dict[str, Any] = {
        "count": len(samples),
        "path": path,
        "mode": mode,
        "pass": kind,
        "wall_ms_median": statistics.median(walls),
        "wall_ms_p90": sorted(walls)[int(0.9 * (len(walls) - 1))],
        "wall_ms_min": min(walls),
        "wall_ms_max": max(walls),
        "open_ms_median": statistics.median(s["open_ms"] for s in samples),
        "backend_storage_bytes_delta_median": statistics.median(
            s["backend_storage_bytes_delta"] for s in samples
        ),
        "samples": samples,
    }
    if backend_name == "grafx":
        summary["grafx_wal_bytes_delta_median"] = statistics.median(
            s["grafx_wal_bytes_delta"] for s in samples
        )
    if instrumented:
        keys: set[str] = set()
        for s in samples:
            keys.update(s["hooks"]["calls"].keys())
        summary["hook_calls_median"] = {
            k: statistics.median(s["hooks"]["calls"].get(k, 0) for s in samples)
            for k in sorted(keys)
        }
        summary["hook_inclusive_ms_median"] = {
            k: statistics.median(
                s["hooks"]["inclusive_ms"].get(k, 0.0) for s in samples
            )
            for k in sorted(keys)
        }
        summary["page_writes_median"] = statistics.median(
            s["hooks"]["page_writes_total"] for s in samples
        )
        summary["page_writes_index_files_median"] = statistics.median(
            s["hooks"]["page_writes_index_files"] for s in samples
        )
        summary["read_view_drops_median"] = {
            k: statistics.median(s["hooks"]["read_view_drops"][k] for s in samples)
            for k in ("all", "file", "doom_pinned")
        }
        summary["unmeasurable_targets"] = sorted(
            {t for s in samples for t in s["hooks"]["unmeasurable_targets"]}
        )
    if phases:
        summary["timers_available"] = all(s["phases"]["available"] for s in samples)
        summary["timers_unavailable_reasons"] = sorted(
            {s["phases"]["reason"] for s in samples if s["phases"]["reason"]}
        )
        for key in (
            "begin_ms",
            "execute_total_ms",
            "execute_count",
            "commit_ms",
            "rollback_ms",
        ):
            summary[f"{key}_median"] = statistics.median(
                s["phases"][key] for s in samples
            )
    return summary


MODE_TEXT: dict[str, str] = {
    "reopen": (
        "reopen-per-op: the backend is closed and reopened before EVERY sample and before its "
        "read-back (attach-fence workaround on grafx 6d9b7a1); open time is EXCLUDED and reported as "
        "open_ms; a warm read of BoardMeta and of the target row is issued unmeasured; caches are "
        "therefore only PARTIALLY warm. DIAGNOSTIC: NOT the gate's warm continuous loop and NOT an "
        "acceptance wall."
    ),
    "continuous": (
        "continuous: one open per family pass (open_ms recorded on sample 0 only), samples "
        "back-to-back on the same handle, read-back on the same handle -- the gate's warm loop "
        "shape. An engine that still has the attach fence (before A1 5002a77) refuses this and the "
        "profile FAILS; nothing is relabelled."
    ),
}


def _require_accel_environment() -> dict[str, Any]:
    """[accel] evidence needs numpy in THIS interpreter; a run without it is refused, not relabelled."""
    try:
        import numpy  # noqa: PLC0415
    except ImportError as failure:
        raise ProfileFailure(
            f"numpy is not importable in {sys.executable}: this interpreter cannot produce [accel] "
            "evidence. Use C:\\Python313\\python.exe with the documented PYTHONPATH; nothing is "
            "installed during a run."
        ) from failure
    environment: dict[str, Any] = {
        "python": sys.executable,
        "python_version": sys.version,
        "numpy": {"version": numpy.__version__, "origin": numpy.__file__},
    }
    try:
        import ladybug  # noqa: PLC0415

        environment["ladybug"] = {
            "version": importlib.metadata.version("ladybug"),
            "origin": ladybug.__file__,
        }
    except Exception as failure:  # noqa: BLE001 — recorded; fatal only for a ladybug run (checked by the caller)
        environment["ladybug"] = {"error": repr(failure)}
    return environment


async def _profile(
    backend_name: str,
    per_family: int,
    path: str,
    out: Path,
    top: int,
    mode: str,
    check_only: bool = False,
    *,
    delete_nodes_types: str = "harness",
    machine_idle_asserted: bool = False,
    run_phases: bool = True,
) -> None:
    if per_family <= 0:
        raise ProfileFailure("per_family must be > 0")  # BLOCKER 7
    if mode not in MODE_TEXT:
        raise ProfileFailure(f"unknown mode {mode!r}")
    if delete_nodes_types not in ("harness", "gate"):
        raise ProfileFailure(f"unknown delete_nodes_types {delete_nodes_types!r}")
    marker = _require_run4_terminal()
    # Clean trees FIRST (fails in milliseconds), import origin right after the modules load.
    checkouts = {
        name: _require_clean_checkout(name, repo)
        for name, repo in (("grafx", GRAFX), ("community", COMMUNITY), ("core", CORE))
    }
    environment = _require_accel_environment()
    if backend_name == "ladybug" and "error" in environment["ladybug"]:
        raise ProfileFailure(
            f"ladybug is not importable in {sys.executable}: {environment['ladybug']['error']}"
        )
    runner, backends = _load_gate_modules()
    checkouts["grafx"]["imported_from"] = _require_imported_from("okto_grafx", GRAFX)
    checkouts["community"]["imported_from"] = _require_imported_from(
        "okto_pulse.community", COMMUNITY
    )
    checkouts["core"]["imported_from"] = _require_imported_from("okto_pulse.core", CORE)
    templates = _templates(runner)
    node_types = (
        _gate_node_types(runner)
        if delete_nodes_types == "gate"
        else list(DELETE_NODES_TYPES_HARNESS)
    )  # H6
    fixtures = Fixtures(
        templates, per_family, path, delete_node_types=tuple(node_types)
    )
    # Only the families this method path can express (store has 7 of the 12); building the digest
    # over ALL families used to raise inside _op before the loop's skip (Codex review item 3).
    families = [f for f in FAMILIES if path == "scope" or f in STORE_CAPABLE]
    # The plan is built ONCE and the very same objects are executed; the digest is over the LOGICAL
    # operation set (family, method, path, payload, postcondition) and deliberately excludes
    # sequence/operation_id, which are run-local counters (Codex: the earlier digest was taken over
    # a throw-away pass whose sequences the real run never reproduced). Same seed => same digest
    # on both backends.
    plan = {f: [fixtures.measured(f, i) for i in range(per_family)] for f in families}
    plan_digest = _plan_digest(plan, families, path)
    forensic_digest, forensic_files, forensic_bytes = _content_digest(
        FORENSIC_WORKSPACE
    )
    machine_state: dict[str, Any] = {
        "before": _machine_state(),
        "after": None,
        "machine_idle_asserted": bool(machine_idle_asserted),
    }  # H5
    report: dict[str, Any] = {
        "backend": backend_name,
        "per_family": per_family,
        "method_path": path,
        "seed": SEED,
        "mode": mode,
        "harness_revision": harness_revision(),
        "operation_set_sha256": plan_digest,
        "operation_set_digest_over": "family, method, path, payload, postcondition (sequence/operation_id excluded)",
        "delete_nodes_types": {
            "mode": delete_nodes_types,
            "types": node_types,
            "note": "the gate form (every manifest node type) changes operation_set_sha256 by design; the harness form keeps the certified digest",
        },
        "run4_terminal_marker": marker,
        "forensic": {
            "workspace": str(FORENSIC_WORKSPACE),
            "content_sha256": forensic_digest,
            "files": forensic_files,
            "bytes": forensic_bytes,
        },
        "checkouts": checkouts,
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
            "machine": platform.machine(),
        },
        "environment": environment,
        "machine_state": machine_state,
        "families_measured": families,
        "measurement_mode": {
            "mode": mode,
            "raw": MODE_TEXT[mode],
            "instrumented": "same as raw plus hooks (installed before the open, H4) + cProfile cumulative and tottime (H1); attribution only; never compare its wall to raw",
            "phases": "same as raw plus three instance timers per op (begin / each execute / commit, H3); no hooks, no cProfile; published beside raw, never in its place",
            "definitive_baseline": "mode=continuous on an engine without the attach fence (A1 5002a77 or later); mode=reopen numbers are diagnostic",
            "hook_attribution": Hooks.ATTRIBUTION_NOTE,
            "hooks_excluded": list(Hooks.EXCLUDED_CONTEXT_MANAGERS),
        },
        "passes": {"raw": {}, "instrumented": {}, "phases": {}},
        "check_only": check_only,
        "notes": [
            "fixtures under private sessions prof-<seed>-*; payload shapes from the manifest templates; identities substituted",
            "every measured op is write-by-construction and read back; projection = empty -> 1 member (tombstoned member restored; "
            "revocation_reason read before and after)",
            "RAW = the operation's wall under the selected --mode, no hooks/cProfile; in mode=reopen it is a "
            "DIAGNOSTIC number (reopen-per-op, caches partially warm), NOT the acceptance wall; only mode="
            "continuous approximates the gate's warm loop. INSTRUMENTED = attribution only; separate copies of "
            "the same fixture base",
            "the copied board is relocated under sha256(PROFILE_RUN_ID)[:24]; DatabaseIdentity carries no path",
            "harness revision h1-h8: RAW pass logically unchanged since the certified source; instrumented/phases "
            "output comparable only across the same harness_revision.sha256",
        ],
    }
    if check_only:
        # Environment, checkouts, import origins, plan digest and mode text -- and NOTHING measured:
        # no board copy, no commit. A check-only report has empty passes and says so.
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(
            f"CHECK ONLY: environment/checkouts/plan verified; operation_set_sha256 {plan_digest}; wrote {out}"
        )
        return
    base_root = SCRATCH / f"m7profile-{backend_name}-base"
    try:
        base = await _build_base(runner, backends, backend_name, fixtures)
        for family in families:
            report["passes"]["raw"][family] = await _run_family(
                runner,
                backends,
                backend_name,
                base,
                family,
                plan[family],
                path=path,
                mode=mode,
                instrumented=False,
                top=top,
            )
            report["passes"]["instrumented"][family] = await _run_family(
                runner,
                backends,
                backend_name,
                base,
                family,
                plan[family],
                path=path,
                mode=mode,
                instrumented=True,
                top=top,
            )
            if run_phases:
                report["passes"]["phases"][family] = await _run_family(
                    runner,
                    backends,
                    backend_name,
                    base,
                    family,
                    plan[family],
                    path=path,
                    mode=mode,
                    instrumented=False,
                    top=top,
                    phases=True,
                )
            raw, ins = (
                report["passes"]["raw"][family],
                report["passes"]["instrumented"][family],
            )
            phase_text = ""
            if run_phases:
                ph = report["passes"]["phases"][family]
                phase_text = f" begin={ph['begin_ms_median']:.1f}ms exec={ph['execute_total_ms_median']:.1f}ms/{ph['execute_count_median']:.0f} commit={ph['commit_ms_median']:.1f}ms"
            print(
                f"{backend_name:8} {family:40} raw={raw['wall_ms_median']:.1f}ms instr={ins['wall_ms_median']:.1f}ms "
                f"pw={ins.get('page_writes_median')} idx={ins.get('page_writes_index_files_median')} "
                f"open={raw['open_ms_median']:.0f}ms storage={raw['backend_storage_bytes_delta_median']}{phase_text}",
                flush=True,
            )
            out.write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )  # checkpoint after each family
    finally:
        if base_root.exists():
            _safe_rmtree(
                base_root
            )  # also on a setup failure, so no copy is left behind
    machine_state["after"] = _machine_state()  # H5
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out} sha256 {hashlib.sha256(out.read_bytes()).hexdigest()}")


def main() -> int:
    global GRAFX, COMMUNITY, CORE, SCRATCH, FORENSIC_WORKSPACE, TERMINAL_MARKER
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--backend", choices=("grafx", "ladybug"), required=True)
    parser.add_argument("--per-family", type=int, default=10)
    parser.add_argument("--method-path", choices=("scope", "store"), default="scope")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--grafx",
        type=Path,
        default=GRAFX,
        help="Grafx checkout the report names; PYTHONPATH must import okto_grafx from it (verified)",
    )
    parser.add_argument(
        "--community",
        type=Path,
        default=COMMUNITY,
        help="Community checkout the report names (default: the tree this harness lives in); its tools/ "
        "and tests/ provide the gate runner and the frozen manifest",
    )
    parser.add_argument(
        "--core",
        type=Path,
        default=CORE,
        help="Core checkout the report names (verified import origin)",
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        default=SCRATCH,
        help="scratch directory holding m7fail/workspace (forensic board copy) and run4.terminal; "
        "every m7profile-* copy is created and deleted under it",
    )
    parser.add_argument(
        "--mode",
        choices=("reopen", "continuous"),
        default="reopen",
        help="reopen: close+reopen the backend before every sample and before its read-back "
        "(attach-fence workaround; diagnostic). continuous: one open per family pass, "
        "samples back-to-back on the same handle, read-back on the same handle (the gate's "
        "warm loop; requires an engine without the attach fence, i.e. A1 or later)",
    )
    parser.add_argument(
        "--delete-nodes-types",
        choices=("harness", "gate"),
        default="harness",
        help="H6: harness = [Decision, Entity] (certified digest); gate = every node type of the "
        "manifest's schema authority (changes operation_set_sha256 by design)",
    )
    parser.add_argument(
        "--machine-idle-asserted",
        action="store_true",
        help="H5: the operator asserts the machine was idle for the whole run (recorded, never inferred)",
    )
    parser.add_argument(
        "--no-phases", action="store_true", help="H3: skip the third (phases) pass"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="verify environment, clean checkouts, import origins and the plan digest, write a "
        "report with EMPTY passes, and stop before any board copy or commit",
    )
    args = parser.parse_args()
    GRAFX = args.grafx.resolve()
    COMMUNITY = args.community.resolve()
    CORE = args.core.resolve()
    SCRATCH = args.scratch.resolve()
    FORENSIC_WORKSPACE = SCRATCH / "m7fail" / "workspace"
    TERMINAL_MARKER = SCRATCH / "run4.terminal"
    try:
        asyncio.run(
            _profile(
                args.backend,
                args.per_family,
                args.method_path,
                args.out,
                args.top,
                args.mode,
                args.check_only,
                delete_nodes_types=args.delete_nodes_types,
                machine_idle_asserted=args.machine_idle_asserted,
                run_phases=not args.no_phases,
            )
        )
    except ProfileFailure as failure:
        print(f"PROFILE FAILED: {failure}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
