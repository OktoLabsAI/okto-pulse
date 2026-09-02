"""Process-isolated crash/recovery harness for the frozen M-PULSE-7 points.

``after_operation`` is an inclusive prefix boundary: exactly that many frozen
trace mutations have completed before a crash seam is armed.  Points 1, 2 and
9 use one additional, reserved-session no-op probe to reach a mutation seam;
the probe is not part of the frozen trace and therefore must not change the
physical graph fingerprint.

The harness deliberately installs no productive crash API.  Every seam is an
in-process wrapper in the disposable crash child, and every hard crash uses
``os._exit``.  A second process reconstructs the productive Community bundle
from the same durable storage and writes the only recovery result accepted by
the supervisor.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
import traceback
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
for _import_root in (REPO_ROOT / "src", REPO_ROOT / "tests", REPO_ROOT / "tools"):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

_RUNNER_SOURCE_PATH: Final[Path] = (
    REPO_ROOT / "tools" / "run_mpulse7_acceptance.py"
).resolve()

_HARNESS_IMPORT_CODE: Final[CodeType] = sys._getframe().f_code
_HARNESS_IMPORT_CODE_FILENAME: Final[str] = _HARNESS_IMPORT_CODE.co_filename
_HARNESS_IMPORT_SOURCE_SHA256: Final[str] = hashlib.sha256(
    Path(__file__).resolve().read_bytes()
).hexdigest()

_CONFIG_FORMAT: Final[str] = "okto-pulse-community-m-pulse-7-crash-config/1"
_HOOK_FORMAT: Final[str] = "okto-pulse-community-m-pulse-7-crash-hook/1"
_PRE_OBSERVATION_FORMAT: Final[str] = (
    "okto-pulse-community-m-pulse-7-pre-crash-observation/1"
)
_RECOVERY_FORMAT: Final[str] = "okto-pulse-community-m-pulse-7-crash-recovery/1"
_HARD_EXIT_CODE: Final[int] = 86
_PROBE_SESSION_ID: Final[str] = "__mpulse7_crash_probe_never_present__"
_WORKER_EXECUTION_AUTHORITY_SHA256: str | None = None


class CrashHarnessError(RuntimeError):
    """The harness could not prove a closed crash/recovery scenario."""


@dataclass(frozen=True, slots=True)
class CrashPointSpec:
    id: str
    after_operation: int
    hook: str
    expected_recovery: str
    seam: str
    fingerprint_observation_phase: str
    requires_production_hook: bool = False

    def manifest_point(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "after_operation": self.after_operation,
            "hook": self.hook,
            "expected_recovery": self.expected_recovery,
        }


CRASH_POINT_SPECS: Final[tuple[CrashPointSpec, ...]] = (
    CrashPointSpec(
        "outbox-prepared-before-provider-call",
        137,
        "after_outbox_prepare_fsync",
        "reconcile_from_authenticated_source_snapshot",
        "CommunityGraphRolloutMutationRecorder.prepare_mutation:after_return",
        "post_recovery",
    ),
    CrashPointSpec(
        "provider-return-before-outbox-terminal",
        1013,
        "after_provider_return",
        "resolve_ambiguous_record_from_source_state",
        "CommunityGraphRolloutMutationRecorder.mark_source_committed:before_call",
        "post_recovery",
    ),
    CrashPointSpec(
        "source-snapshot-close-before-candidate-open",
        2003,
        "after_source_snapshot_close",
        "abandon_unpublished_candidate_and_resume_shadow",
        "CommunityBoardGraphShadowCycleAdapter._connector:first_read_only_call",
        "post_recovery",
    ),
    CrashPointSpec(
        "candidate-write-before-certificate",
        2499,
        "after_candidate_checkpoint",
        "cold_verify_before_any_publication",
        "CommunityBoardGraphShadowCycleAdapter._sink_factory:checkpoint_after_return",
        "post_recovery",
    ),
    CrashPointSpec(
        "checkpoint-fsync-before-canary-ready",
        2500,
        "after_shadow_checkpoint_fsync",
        "resume_from_authenticated_checkpoint",
        "CommunityGraphRolloutJournal.reconcile_snapshot:after_return",
        "post_recovery",
    ),
    CrashPointSpec(
        "final-delta-before-binding-cas",
        7499,
        "after_final_delta_certificate",
        "keep_ladybug_active_and_retry_freeze",
        "CommunityGraphRolloutJournal.record_comparison_receipt:after_return",
        "post_recovery",
    ),
    CrashPointSpec(
        "binding-replace-before-directory-fsync",
        7500,
        "after_binding_replace",
        "authenticate_persisted_binding_without_fallback",
        "graph_backend_binding.fsync_directory:binding_parent_before_call",
        "post_recovery",
    ),
    CrashPointSpec(
        "binding-cas-before-rollout-transition",
        7501,
        "after_binding_cas_fsync",
        "derive_active_backend_from_binding_and_reconcile_rollout",
        "CommunityGraphBackendBindingStore.compare_and_swap_board_binding:after_return",
        "post_recovery",
    ),
    CrashPointSpec(
        "rollback-close-before-first-grafx-write",
        8001,
        "after_rollback_close_fsync",
        "keep_rollback_closed_before_accepting_write",
        "CommunityGraphRolloutMutationRecorder.prepare_mutation:after_return",
        "post_recovery",
    ),
    CrashPointSpec(
        "privacy-invalidation-before-copy-sweep",
        9999,
        "after_privacy_invalidation_fsync",
        "resume_erasure_without_rehydration",
        "CommunityGraphRolloutJournal.close_for_privacy:after_return",
        "pre_invalidation",
    ),
    CrashPointSpec(
        "copy-sweep-before-absence-receipt",
        10000,
        "after_all_copy_sweeps",
        "reverify_aggregate_absence_before_success",
        "CommunityGraphRolloutJournal.erase_privacy_storage:before_call",
        "pre_invalidation",
    ),
)
_SPECS_BY_ID: Final[dict[str, CrashPointSpec]] = {
    spec.id: spec for spec in CRASH_POINT_SPECS
}


@dataclass(frozen=True, slots=True)
class ProcessResult:
    pid: int
    exit_code: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class _Bundle:
    context: Any
    storage_root: Path
    previous_registry: Any
    composition: Any
    routed: Any
    board: Any

    @property
    def semantic_store(self) -> Any:
        return self.board.graph_store

    @property
    def graph_transaction(self) -> Any:
        return self.board.graph_transaction


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise CrashHarnessError(reason)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _certification_runner_module() -> ModuleType:
    """Resolve the exact runner already executing, including CLI ``__main__``."""

    expected_origin = os.path.normcase(os.path.realpath(_RUNNER_SOURCE_PATH))
    candidates: dict[int, ModuleType] = {}
    for candidate in tuple(sys.modules.values()):
        if not isinstance(candidate, ModuleType):
            continue
        raw_origin = getattr(candidate, "__file__", None)
        if not isinstance(raw_origin, (str, bytes, os.PathLike)):
            continue
        if os.path.normcase(os.path.realpath(raw_origin)) == expected_origin:
            candidates[id(candidate)] = candidate
    _require(len(candidates) <= 1, "runner_authority_module_ambiguous")
    if candidates:
        return next(iter(candidates.values()))

    try:
        imported = importlib.import_module("run_mpulse7_acceptance")
    except ImportError as failure:
        raise CrashHarnessError("runner_authority_module_unavailable") from failure
    imported_origin = getattr(imported, "__file__", None)
    _require(
        isinstance(imported_origin, (str, bytes, os.PathLike))
        and os.path.normcase(os.path.realpath(imported_origin)) == expected_origin,
        "runner_authority_module_origin_invalid",
    )
    return imported


def _validate_harness_import_authority() -> dict[str, str]:
    """Reject source restored after this process loaded different harness code."""

    origin = Path(__file__).resolve()
    try:
        runner = _certification_runner_module()
        code_object_sha256 = getattr(runner, "_code_object_sha256", None)
        _require(callable(code_object_sha256), "runner_code_authority_api_missing")

        source = origin.read_bytes()
        current_code = compile(
            source,
            _HARNESS_IMPORT_CODE_FILENAME,
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
    except (OSError, SyntaxError, TypeError, ValueError) as failure:
        raise CrashHarnessError("harness_import_authority_unreadable") from failure
    _require(
        isinstance(current_code, CodeType),
        "harness_import_authority_code_missing",
    )
    source_sha256 = hashlib.sha256(source).hexdigest()
    code_sha256 = code_object_sha256(current_code)
    loaded_code_sha256 = code_object_sha256(_HARNESS_IMPORT_CODE)
    _require(
        source_sha256 == _HARNESS_IMPORT_SOURCE_SHA256,
        "harness_imported_source_bytes_changed",
    )
    _require(
        code_sha256 == loaded_code_sha256,
        "harness_loaded_code_differs_from_source",
    )
    return {"code_sha256": code_sha256, "source_sha256": source_sha256}


def _relaxed_process_authority() -> dict[str, Any]:
    """Collect actual origins for non-certifying tests without requiring clean Git."""

    import importlib
    import inspect

    records: dict[str, Any] = {}
    for module_name in (
        "mpulse7_crash_harness",
        "mpulse7_acceptance_backends",
        "run_mpulse7_acceptance",
        "okto_pulse.core.kg.logical_transfer",
        "okto_grafx",
    ):
        module = importlib.import_module(module_name)
        source = inspect.getsourcefile(module) or getattr(module, "__file__", None)
        _require(source is not None, f"process_authority_origin_missing:{module_name}")
        origin = Path(source).resolve()
        try:
            source_sha256 = hashlib.sha256(origin.read_bytes()).hexdigest()
        except OSError as failure:
            raise CrashHarnessError(
                f"process_authority_origin_unreadable:{module_name}"
            ) from failure
        git = subprocess.run(
            ["git", "-C", str(origin.parent), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        records[module_name] = {
            "origin": str(origin),
            "sha256": source_sha256,
            "git_head": git.stdout.strip() if git.returncode == 0 else None,
        }
    return {"format": "mpulse7-test-process-authority/1", "modules": records}


def _collect_process_authority_sha256(*, certification: bool) -> str:
    """Hash the source authority observed by this exact Python process."""

    if certification:
        _validate_harness_import_authority()
        runner = _certification_runner_module()
        canonical_sha256 = getattr(runner, "canonical_sha256", None)
        collect_process_authority = getattr(
            runner,
            "collect_certification_process_authority",
            None,
        )
        _require(callable(canonical_sha256), "runner_digest_authority_api_missing")
        _require(
            callable(collect_process_authority),
            "runner_process_authority_api_missing",
        )

        authority = collect_process_authority()
        digest = canonical_sha256(authority)
    else:
        digest = _canonical_sha256(_relaxed_process_authority())
    _require(_is_sha256(digest), "crash_execution_authority_digest_invalid")
    return digest


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Publish harness evidence durably without depending on productive hooks."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(value), stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json_object(path: Path, *, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as failure:
        raise CrashHarnessError(reason) from failure
    _require(type(value) is dict, reason)
    return value


def _validate_point(point: Mapping[str, Any]) -> CrashPointSpec:
    _require(type(point) is dict, "crash_point_not_plain_object")
    _require(
        set(point) == {"id", "after_operation", "hook", "expected_recovery"},
        "crash_point_shape_drift",
    )
    point_id = point.get("id")
    _require(type(point_id) is str, "crash_point_id_invalid")
    spec = _SPECS_BY_ID.get(point_id)
    _require(spec is not None, "crash_point_not_frozen")
    assert spec is not None
    _require(point == spec.manifest_point(), f"crash_point_coordinate_drift:{point_id}")
    _require(
        not spec.requires_production_hook, f"productive_crash_hook_required:{point_id}"
    )
    return spec


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    stdout, stderr = process.communicate()
    return ProcessResult(
        pid=process.pid,
        exit_code=int(process.returncode),
        stdout=stdout,
        stderr=stderr,
    )


def _worker_command(mode: str, config_path: Path) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        mode,
        "--config",
        str(config_path),
    ]


def _scenario_root(backend: object, spec: CrashPointSpec) -> Path:
    context = getattr(backend, "_context", None)
    workspace_value = getattr(context, "workspace", None)
    run_id = getattr(context, "run_id", None)
    _require(type(workspace_value) is str, "crash_supervisor_workspace_missing")
    _require(type(run_id) is str and bool(run_id), "crash_supervisor_run_id_missing")
    workspace = Path(os.path.abspath(workspace_value))
    _require(workspace.is_absolute(), "crash_supervisor_workspace_not_absolute")
    digest = hashlib.sha256(
        f"{run_id}\0{spec.id}\0{uuid.uuid4().hex}".encode()
    ).hexdigest()[:24]
    root = workspace / ".mp7" / "c" / digest
    try:
        root.relative_to(workspace)
    except (
        ValueError
    ) as failure:  # pragma: no cover - all segments are constants/digest
        raise CrashHarnessError("crash_scenario_root_escapes_workspace") from failure
    root.mkdir(parents=True, exist_ok=False)
    return root


def _closed_receipt(
    *,
    spec: CrashPointSpec,
    crash: ProcessResult,
    recovery: ProcessResult,
    hook_evidence: Mapping[str, Any],
    recovered: Mapping[str, Any],
    certification: bool,
) -> dict[str, Any]:
    expected_recovery_keys = {
        "absence_verified",
        "fingerprint_logical_graph_sha256",
        "fingerprint_observation_phase",
        "fingerprint_trace_model_sha256",
        "format",
        "observed_recovery",
        "recovered",
        "recovered_generation",
        "recovered_storage_identity",
        "verify_ok",
        "verify_scope",
        "worker_pid",
        "execution_authority_sha256",
    }
    _require(set(recovered) == expected_recovery_keys, "recovery_result_shape_open")
    _require(recovered["format"] == _RECOVERY_FORMAT, "recovery_result_format_invalid")
    receipt = {
        "id": spec.id,
        "hook": spec.hook,
        "after_operation": spec.after_operation,
        "expected_recovery": spec.expected_recovery,
        "observed_recovery": recovered["observed_recovery"],
        "crash_process_pid": hook_evidence["pid"],
        "recovery_process_pid": recovered["worker_pid"],
        "crash_exit_code": crash.exit_code,
        "recovered": recovered["recovered"],
        "recovered_storage_identity": recovered["recovered_storage_identity"],
        "recovered_generation": recovered["recovered_generation"],
        "verify_ok": recovered["verify_ok"],
        "verify_scope": recovered["verify_scope"],
        "fingerprint_trace_model_sha256": recovered["fingerprint_trace_model_sha256"],
        "fingerprint_logical_graph_sha256": recovered[
            "fingerprint_logical_graph_sha256"
        ],
        "absence_verified": recovered["absence_verified"],
        "fingerprint_observation_phase": recovered["fingerprint_observation_phase"],
    }
    if certification:
        receipt.update(
            {
                "crash_execution_authority_sha256": hook_evidence[
                    "execution_authority_sha256"
                ],
                "recovery_execution_authority_sha256": recovered[
                    "execution_authority_sha256"
                ],
            }
        )
    return receipt


def _run_scenario(backend: object, point: Mapping[str, Any]) -> dict[str, Any]:
    spec = _validate_point(point)
    context = getattr(backend, "_context", None)
    _require(
        getattr(context, "backend", None) == "ladybug", "crash_harness_requires_ladybug"
    )
    board_id = getattr(context, "board_id", None)
    _require(
        type(board_id) is str and bool(board_id), "crash_supervisor_board_id_invalid"
    )
    supplied_authority = getattr(
        context, "certification_process_authority_sha256", None
    )
    _require(
        supplied_authority is None or _is_sha256(supplied_authority),
        "crash_supervisor_authority_invalid",
    )
    certification = supplied_authority is not None
    parent_authority = _collect_process_authority_sha256(certification=certification)
    if supplied_authority is not None:
        _require(
            parent_authority == supplied_authority,
            "crash_supervisor_authority_differs_from_gate",
        )
    expected_authority = supplied_authority or parent_authority

    scenario_root = _scenario_root(backend, spec)
    config_path = scenario_root / "harness-config.json"
    config = {
        "format": _CONFIG_FORMAT,
        "point": spec.manifest_point(),
        "scenario_root": str(scenario_root),
        "board_id": board_id,
        "run_id": f"mpulse7-crash-{uuid.uuid4().hex}",
        "manifest_path": str(
            REPO_ROOT / "tests" / "fixtures" / "m_pulse_7_acceptance_gate_v3.json"
        ),
        "hook_evidence_path": str(scenario_root / "hook-evidence.json"),
        "pre_observation_path": str(scenario_root / "pre-observation.json"),
        "recovery_result_path": str(scenario_root / "recovery-result.json"),
        "expected_execution_authority_sha256": expected_authority,
        "certification": certification,
    }
    _write_json_atomic(config_path, config)

    crash = _run_process(_worker_command("crash", config_path), cwd=REPO_ROOT)
    if crash.exit_code != _HARD_EXIT_CODE:
        raise CrashHarnessError(
            f"crash_child_did_not_hard_exit:{spec.id}:exit={crash.exit_code}:"
            f"stderr={crash.stderr[-4000:]}"
        )
    hook_evidence = _require_hook_evidence(config, spec)
    recovery = _run_process(_worker_command("recovery", config_path), cwd=REPO_ROOT)
    if recovery.exit_code != 0:
        raise CrashHarnessError(
            f"recovery_child_failed:{spec.id}:exit={recovery.exit_code}:"
            f"stderr={recovery.stderr[-4000:]}"
        )
    recovered = _read_json_object(
        Path(config["recovery_result_path"]), reason="recovery_result_missing"
    )
    crash_worker_pid = hook_evidence["pid"]
    recovery_worker_pid = recovered.get("worker_pid")
    _require(
        hook_evidence["execution_authority_sha256"]
        == recovered.get("execution_authority_sha256")
        == expected_authority,
        "crash_child_execution_authority_mismatch",
    )
    _require(
        type(crash_worker_pid) is int
        and crash_worker_pid > 0
        and type(recovery_worker_pid) is int
        and recovery_worker_pid > 0,
        "crash_process_pid_invalid",
    )
    _require(
        crash_worker_pid != recovery_worker_pid,
        "crash_and_recovery_process_not_distinct",
    )
    return _closed_receipt(
        spec=spec,
        crash=crash,
        recovery=recovery,
        hook_evidence=hook_evidence,
        recovered=recovered,
        certification=bool(config["certification"]),
    )


async def run_crash_point(backend: object, point: Mapping[str, Any]) -> dict[str, Any]:
    """Run one frozen point without mutating the gate supervisor's storage."""

    return await asyncio.to_thread(_run_scenario, backend, dict(point))


def _load_config(path: Path) -> tuple[dict[str, Any], CrashPointSpec]:
    config = _read_json_object(path, reason="crash_config_unreadable")
    _require(config.get("format") == _CONFIG_FORMAT, "crash_config_format_invalid")
    expected_keys = {
        "board_id",
        "certification",
        "expected_execution_authority_sha256",
        "format",
        "hook_evidence_path",
        "manifest_path",
        "point",
        "pre_observation_path",
        "recovery_result_path",
        "run_id",
        "scenario_root",
    }
    _require(set(config) == expected_keys, "crash_config_shape_open")
    point = config.get("point")
    _require(type(point) is dict, "crash_config_point_invalid")
    spec = _validate_point(point)
    _require(
        type(config["certification"]) is bool, "crash_config_certification_invalid"
    )
    _require(
        _is_sha256(config["expected_execution_authority_sha256"]),
        "crash_config_execution_authority_invalid",
    )
    root = Path(str(config["scenario_root"]))
    _require(root.is_absolute(), "crash_scenario_root_not_absolute")
    root = Path(os.path.abspath(root))
    for key in ("hook_evidence_path", "pre_observation_path", "recovery_result_path"):
        candidate = Path(str(config[key]))
        _require(candidate.is_absolute(), f"crash_config_{key}_not_absolute")
        try:
            candidate.relative_to(root)
        except ValueError as failure:
            raise CrashHarnessError(f"crash_config_{key}_escapes_root") from failure
    return config, spec


async def _build_bundle(
    config: Mapping[str, Any],
    *,
    initialize_if_missing: bool,
    allow_missing_binding: bool = False,
) -> _Bundle:
    from mpulse7_acceptance_backends import _settings
    from okto_pulse.core.infra.config import configure_settings
    from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
    from okto_pulse.core.kg.interfaces.registry import (
        capture_registry_state_for_tests,
        restore_registry_state_for_tests,
    )

    runner = _certification_runner_module()
    GateBackendContext = getattr(runner, "GateBackendContext", None)
    _require(GateBackendContext is not None, "runner_backend_context_api_missing")

    from okto_pulse.community.adapters.composition import (
        build_community_kg_composition,
    )
    from okto_pulse.community.adapters.graph_backend_binding import (
        CommunityGraphBackendBindingStore,
    )

    scenario_root = Path(str(config["scenario_root"]))
    kg_root = scenario_root / "kg"
    board_id = str(config["board_id"])
    inspector = CommunityGraphBackendBindingStore(kg_root)
    try:
        persisted = inspector.inspect_board_binding(board_id)
    except GraphCapabilityUnavailable as failure:
        if failure.details.get("reason") != "binding_missing":
            raise
        persisted = None
    backend = "ladybug" if persisted is None else persisted.backend
    context = GateBackendContext(
        backend=backend,
        board_id=board_id,
        workspace=str(scenario_root),
        run_id=str(config["run_id"]),
    )
    previous = capture_registry_state_for_tests()
    try:
        settings = _settings(scenario_root, backend)
        configure_settings(settings)
        composition = build_community_kg_composition(
            upload_dir=settings.upload_dir,
            settings=settings,
        )
        registry = composition.base_registry
        registry.config = settings
        restore_registry_state_for_tests(registry)
        routed = composition.routed_graph
        _require(routed is not None, "crash_community_routed_graph_missing")
        board = routed.board
        try:
            binding = board.binding_store.inspect_board_binding(board_id)
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") != "binding_missing":
                raise
            binding = None
        if binding is None:
            if initialize_if_missing:
                await routed.graph_schema_manager.ensure_bootstrapped(board_id)
            else:
                _require(allow_missing_binding, "crash_recovery_binding_missing")
        return _Bundle(
            context=context,
            storage_root=scenario_root,
            previous_registry=previous,
            composition=composition,
            routed=routed,
            board=board,
        )
    except BaseException:
        restore_registry_state_for_tests(previous)
        raise


async def _close_bundle(bundle: _Bundle) -> None:
    from okto_pulse.core.kg.interfaces.registry import restore_registry_state_for_tests

    try:
        await bundle.board.graph_lifecycle.close(None)
        bundle.board.grafx_pool.close_all()
    finally:
        restore_registry_state_for_tests(bundle.previous_registry)


def _observe(bundle: _Bundle) -> dict[str, str]:
    from mpulse7_acceptance_backends import _observe_snapshot

    from okto_pulse.community.adapters import kg_runtime
    from okto_pulse.community.adapters.logical_transfer_factories import (
        SCOPE_BOARD,
        make_grafx_logical_source,
        make_ladybug_logical_source,
    )

    binding = bundle.board.binding_store.acquire_board_binding(bundle.context.board_id)
    if binding.backend == "ladybug":
        with kg_runtime.registered_raw_connection(bundle.context.board_id) as opened:
            database, _connection = opened
            snapshot = make_ladybug_logical_source(
                database,
                scope=SCOPE_BOARD,
            ).open_snapshot()
            try:
                observed = _observe_snapshot(snapshot)
            finally:
                snapshot.close()
    else:
        _require(binding.page_size is not None, "crash_grafx_page_size_missing")
        temporary_parent = bundle.storage_root / "logical-snapshot-temp"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with bundle.board.grafx_pool.acquire(
            binding.physical_path,
            page_size=binding.page_size,
        ) as lease:
            source = make_grafx_logical_source(
                lease.database,
                scope=SCOPE_BOARD,
                scan_batch_size=500,
                temporary_parent=temporary_parent,
            )
            snapshot = source.open_snapshot()
            try:
                observed = _observe_snapshot(snapshot)
            finally:
                snapshot.close()
    return {
        "fingerprint_trace_model_sha256": observed.trace_model_sha256,
        "fingerprint_logical_graph_sha256": observed.logical_graph_sha256,
    }


async def _verify_all(bundle: _Bundle) -> None:
    from okto_pulse.community.adapters import kg_runtime

    validation = await bundle.routed.graph_schema_manager.validate(
        bundle.context.board_id
    )
    _require(validation.valid is True, "crash_recovered_schema_invalid")
    binding = bundle.board.binding_store.acquire_board_binding(bundle.context.board_id)
    if binding.backend == "grafx":
        _require(binding.page_size is not None, "crash_grafx_page_size_missing")
        with bundle.board.grafx_pool.acquire(
            binding.physical_path,
            page_size=binding.page_size,
        ) as lease:
            report = lease.database.verify("all")
            _require(
                report.scope == "all" and report.clean is True,
                "crash_grafx_verify_failed",
            )
    else:
        health = kg_runtime.verify_kuzu_db_health(bundle.context.board_id)
        _require(health.get("ok") is True, "crash_ladybug_verify_failed")


async def _reopen_at_recovery_boundary(
    config: Mapping[str, Any],
    bundle: _Bundle,
    *,
    after_operations: int,
    expected_trace_fingerprint: str,
) -> _Bundle:
    """Reproduce one frozen cold-recovery cycle while replaying a crash prefix."""

    _require(type(after_operations) is int, "crash_recovery_boundary_invalid")
    _require(
        _is_sha256(expected_trace_fingerprint),
        "crash_recovery_fingerprint_invalid",
    )
    expected_identity = _identity(bundle)
    await _close_bundle(bundle)

    reopened: _Bundle | None = None
    try:
        reopened = await _build_bundle(config, initialize_if_missing=False)
        _require(
            _collect_process_authority_sha256(
                certification=bool(config["certification"])
            )
            == config["expected_execution_authority_sha256"],
            "crash_recovery_boundary_execution_authority_mismatch",
        )
        recovery = await reopened.board.graph_recovery.recover_wal_only(
            reopened.context.board_id
        )
        _require(
            recovery.status in {"recovered", "skipped"}
            and recovery.main_untouched is True,
            "crash_recovery_boundary_wal_recovery_failed",
        )
        opened = await reopened.board.graph_lifecycle.open(reopened.context.board_id)
        _require(
            opened.opened is True,
            "crash_recovery_boundary_post_recovery_reopen_failed",
        )
        await _verify_all(reopened)
        _require(
            _identity(reopened) == expected_identity,
            "crash_recovery_boundary_storage_identity_changed",
        )
        observed = _observe(reopened)
        _require(
            observed["fingerprint_trace_model_sha256"] == expected_trace_fingerprint,
            f"crash_recovery_boundary_{after_operations}_diverged",
        )
        return reopened
    except BaseException:
        if reopened is not None:
            try:
                await _close_bundle(reopened)
            except Exception as cleanup:  # noqa: BLE001 - preserve primary failure
                traceback.print_exception(cleanup, file=sys.stderr)
        raise


def _identity(bundle: _Bundle) -> tuple[str, str]:
    binding = bundle.board.binding_store.acquire_board_binding(bundle.context.board_id)
    storage_identity = _canonical_sha256(
        {
            "backend": binding.backend,
            "binding_sha256": binding.binding_sha256,
            "generation": binding.generation,
            "run_id_sha256": hashlib.sha256(
                bundle.context.run_id.encode("utf-8")
            ).hexdigest(),
        }
    )
    return storage_identity, binding.generation


def _expected_replay_capture_high_water(
    *,
    after_operation: int,
    recovery_boundaries: frozenset[int],
) -> int:
    """Account for the fenced administrative recovery writes in a prefix."""

    _require(
        type(after_operation) is int and after_operation > 0,
        "crash_capture_after_invalid",
    )
    _require(
        all(type(boundary) is int and boundary > 0 for boundary in recovery_boundaries),
        "crash_capture_recovery_boundaries_invalid",
    )
    return after_operation + sum(
        boundary <= after_operation for boundary in recovery_boundaries
    )


def _rollout_capture_high_water(bundle: _Bundle) -> int:
    from okto_pulse.community.adapters.graph_rollout_journal import (
        CommunityGraphRolloutJournal,
    )

    journal = CommunityGraphRolloutJournal(
        bundle.board.binding_store.root,
        bundle.context.board_id,
    )
    high_water = journal.capture_high_water()
    _require(
        type(high_water) is int and high_water > 0,
        "crash_rollout_capture_high_water_invalid",
    )
    return high_water


def _hard_exit(config: Mapping[str, Any], spec: CrashPointSpec) -> None:
    authority = _WORKER_EXECUTION_AUTHORITY_SHA256
    _require(_is_sha256(authority), "crash_worker_authority_not_captured")
    marker = {
        "format": _HOOK_FORMAT,
        "id": spec.id,
        "hook": spec.hook,
        "after_operation": spec.after_operation,
        "seam": spec.seam,
        "pid": os.getpid(),
        "execution_authority_sha256": authority,
    }
    _write_json_atomic(Path(str(config["hook_evidence_path"])), marker)
    os._exit(_HARD_EXIT_CODE)


def _arm_and_crash(
    bundle: _Bundle, config: Mapping[str, Any], spec: CrashPointSpec
) -> None:
    from okto_pulse.community.adapters import graph_backend_binding
    from okto_pulse.community.adapters.graph_rollout_journal import (
        CommunityGraphRolloutJournal,
        CommunityGraphRolloutMutationRecorder,
    )
    from okto_pulse.community.adapters.logical_transfer_factories import (
        make_grafx_logical_sink,
    )

    board_id = bundle.context.board_id
    coordinator = bundle.board.graph_rollout_coordinator

    if spec.id in {
        "outbox-prepared-before-provider-call",
        "rollback-close-before-first-grafx-write",
    }:
        original = CommunityGraphRolloutMutationRecorder.prepare_mutation

        def after_prepare(recorder: Any, **kwargs: Any) -> Any:
            token = original(recorder, **kwargs)
            if kwargs.get("board_id") == board_id and token is not None:
                _hard_exit(config, spec)
            return token

        CommunityGraphRolloutMutationRecorder.prepare_mutation = after_prepare
        bundle.semantic_store.delete_edges_by_session(board_id, _PROBE_SESSION_ID)
        raise CrashHarnessError(f"crash_hook_not_reached:{spec.id}")

    if spec.id == "provider-return-before-outbox-terminal":
        original = CommunityGraphRolloutMutationRecorder.mark_source_committed

        def before_terminal(recorder: Any, token: Any) -> None:
            if getattr(token, "board_id", None) == board_id:
                _hard_exit(config, spec)
            original(recorder, token)

        CommunityGraphRolloutMutationRecorder.mark_source_committed = before_terminal
        bundle.semantic_store.delete_edges_by_session(board_id, _PROBE_SESSION_ID)
        raise CrashHarnessError(f"crash_hook_not_reached:{spec.id}")

    shadow = coordinator._shadow
    if spec.id == "source-snapshot-close-before-candidate-open":

        def before_candidate_open(*_args: Any, **kwargs: Any) -> Any:
            _require(kwargs.get("read_only") is True, "candidate_open_not_read_only")
            _hard_exit(config, spec)

        shadow._connector = before_candidate_open
        coordinator.run_shadow_cycle(board_id)
        raise CrashHarnessError(f"crash_hook_not_reached:{spec.id}")

    if spec.id == "candidate-write-before-certificate":

        class _CheckpointCrashSink:
            def __init__(self, delegate: Any) -> None:
                self._delegate = delegate

            def checkpoint(self) -> None:
                self._delegate.checkpoint()
                _hard_exit(config, spec)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._delegate, name)

        def sink_factory(*args: Any, **kwargs: Any) -> Any:
            return _CheckpointCrashSink(make_grafx_logical_sink(*args, **kwargs))

        shadow._sink_factory = sink_factory
        coordinator.run_shadow_cycle(board_id)
        raise CrashHarnessError(f"crash_hook_not_reached:{spec.id}")

    if spec.id in {
        "checkpoint-fsync-before-canary-ready",
        "final-delta-before-binding-cas",
    }:
        method_name = (
            "reconcile_snapshot"
            if spec.id == "checkpoint-fsync-before-canary-ready"
            else "record_comparison_receipt"
        )
        original = getattr(CommunityGraphRolloutJournal, method_name)

        def after_journal_commit(journal: Any, *args: Any, **kwargs: Any) -> Any:
            result = original(journal, *args, **kwargs)
            if journal.board_id == board_id:
                _hard_exit(config, spec)
            return result

        setattr(CommunityGraphRolloutJournal, method_name, after_journal_commit)
        coordinator.run_shadow_cycle(board_id)
        raise CrashHarnessError(f"crash_hook_not_reached:{spec.id}")

    if spec.id in {
        "binding-replace-before-directory-fsync",
        "binding-cas-before-rollout-transition",
    }:
        completed = coordinator.run_shadow_cycle(board_id)
        _require(completed.receipt is not None, "pre_cutover_shadow_receipt_missing")
        if spec.id == "binding-replace-before-directory-fsync":
            original_fsync = graph_backend_binding.fsync_directory
            binding_parent = bundle.board.binding_store._board_binding_path(
                board_id
            ).parent

            def before_binding_directory_fsync(path: Any) -> None:
                if Path(path) == binding_parent:
                    _hard_exit(config, spec)
                original_fsync(path)

            graph_backend_binding.fsync_directory = before_binding_directory_fsync
        else:
            store = bundle.board.binding_store
            original_cas = store.compare_and_swap_board_binding

            def after_binding_cas(**kwargs: Any) -> Any:
                result = original_cas(**kwargs)
                if kwargs.get("board_id") == board_id:
                    _hard_exit(config, spec)
                return result

            store.compare_and_swap_board_binding = after_binding_cas
        coordinator.promote(board_id)
        raise CrashHarnessError(f"crash_hook_not_reached:{spec.id}")

    if spec.id == "rollback-close-before-first-grafx-write":  # pragma: no cover
        raise AssertionError("handled by the shared prepare seam")

    if spec.id in {
        "privacy-invalidation-before-copy-sweep",
        "copy-sweep-before-absence-receipt",
    }:
        completed = coordinator.run_shadow_cycle(board_id)
        _require(completed.receipt is not None, "privacy_shadow_copy_missing")
        if spec.id == "privacy-invalidation-before-copy-sweep":
            original_close = CommunityGraphRolloutJournal.close_for_privacy

            def after_invalidation(journal: Any, *args: Any, **kwargs: Any) -> Any:
                result = original_close(journal, *args, **kwargs)
                if journal.board_id == board_id:
                    _hard_exit(config, spec)
                return result

            CommunityGraphRolloutJournal.close_for_privacy = after_invalidation
        else:
            original_erase = CommunityGraphRolloutJournal.erase_privacy_storage

            def before_absence_receipt(journal: Any, *args: Any, **kwargs: Any) -> Any:
                if journal.board_id == board_id:
                    _hard_exit(config, spec)
                return original_erase(journal, *args, **kwargs)

            CommunityGraphRolloutJournal.erase_privacy_storage = before_absence_receipt
        bundle.board.graph_runtime_store.erase_board_graph(
            board_id,
            reason="mpulse7_crash_acceptance",
        )
        raise CrashHarnessError(f"crash_hook_not_reached:{spec.id}")

    raise CrashHarnessError(f"crash_point_unmapped:{spec.id}")


async def _crash_worker(config: Mapping[str, Any], spec: CrashPointSpec) -> None:
    global _WORKER_EXECUTION_AUTHORITY_SHA256

    _WORKER_EXECUTION_AUTHORITY_SHA256 = _collect_process_authority_sha256(
        certification=bool(config["certification"])
    )
    _require(
        _WORKER_EXECUTION_AUTHORITY_SHA256
        == config["expected_execution_authority_sha256"],
        "crash_worker_execution_authority_mismatch",
    )
    runner = _certification_runner_module()
    execute_operation = getattr(runner, "_execute_operation", None)
    verify_frozen_inputs = getattr(runner, "verify_frozen_inputs", None)
    _require(callable(execute_operation), "runner_execute_operation_api_missing")
    _require(callable(verify_frozen_inputs), "runner_frozen_inputs_api_missing")

    inputs = verify_frozen_inputs(Path(str(config["manifest_path"])))
    manifest_points = inputs.manifest["crash_points"]["points"]
    _require(
        [candidate for candidate in manifest_points if candidate["id"] == spec.id]
        == [spec.manifest_point()],
        "runtime_manifest_crash_point_drift",
    )
    bundle = await _build_bundle(config, initialize_if_missing=True)
    _require(
        _collect_process_authority_sha256(certification=bool(config["certification"]))
        == config["expected_execution_authority_sha256"],
        "crash_bundle_execution_authority_mismatch",
    )
    coordinator = bundle.board.graph_rollout_coordinator
    rollout = coordinator.start(bundle.context.board_id)
    _require(rollout.state == "shadowing", "crash_rollout_did_not_start")
    # The productive Grafx sink requires its stable generations container to
    # exist, while the protocol requires only the selected generation itself to
    # remain absent.  Preparing this container changes no graph state and keeps
    # the candidate path available for the coordinator's absence checks.
    rollout.candidate.physical_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoints = {
        int(checkpoint["after_operations"]): checkpoint
        for checkpoint in inputs.manifest["trace"]["checkpoints"]
    }
    recovery_boundaries = frozenset(
        int(value)
        for value in inputs.manifest["reopen_recovery_cycles"]["after_operations"]
    )
    for operation in inputs.operations[: spec.after_operation]:
        await execute_operation(bundle, bundle.context, operation)
        sequence = int(operation["sequence"])
        if sequence not in recovery_boundaries:
            continue
        checkpoint = checkpoints.get(sequence)
        _require(checkpoint is not None, "crash_recovery_checkpoint_missing")
        bundle = await _reopen_at_recovery_boundary(
            config,
            bundle,
            after_operations=sequence,
            expected_trace_fingerprint=str(checkpoint["model_fingerprint_sha256"]),
        )
        coordinator = bundle.board.graph_rollout_coordinator

    capture_high_water = _rollout_capture_high_water(bundle)
    _require(
        capture_high_water
        == _expected_replay_capture_high_water(
            after_operation=spec.after_operation,
            recovery_boundaries=recovery_boundaries,
        ),
        "crash_replay_capture_high_water_mismatch",
    )
    observed = _observe(bundle)
    _write_json_atomic(
        Path(str(config["pre_observation_path"])),
        {
            "format": _PRE_OBSERVATION_FORMAT,
            "id": spec.id,
            "after_operation": spec.after_operation,
            "rollout_capture_high_water": capture_high_water,
            **observed,
        },
    )

    if spec.id == "rollback-close-before-first-grafx-write":
        completed = coordinator.run_shadow_cycle(bundle.context.board_id)
        _require(completed.receipt is not None, "rollback_close_shadow_receipt_missing")
        promoted = coordinator.promote(bundle.context.board_id)
        _require(
            promoted.state == "grafx_active_rollback_open",
            "rollback_close_cutover_not_open",
        )
    _arm_and_crash(bundle, config, spec)


def _require_hook_evidence(
    config: Mapping[str, Any], spec: CrashPointSpec
) -> dict[str, Any]:
    marker = _read_json_object(
        Path(str(config["hook_evidence_path"])), reason="crash_hook_evidence_missing"
    )
    expected = {
        "format": _HOOK_FORMAT,
        "id": spec.id,
        "hook": spec.hook,
        "after_operation": spec.after_operation,
        "seam": spec.seam,
        "pid": marker.get("pid"),
        "execution_authority_sha256": config["expected_execution_authority_sha256"],
    }
    _require(marker == expected, "crash_hook_evidence_invalid")
    _require(type(marker["pid"]) is int and marker["pid"] > 0, "crash_hook_pid_invalid")
    return marker


def _pre_observation(config: Mapping[str, Any], spec: CrashPointSpec) -> dict[str, Any]:
    observed = _read_json_object(
        Path(str(config["pre_observation_path"])),
        reason="pre_crash_physical_observation_missing",
    )
    _require(
        observed.get("format") == _PRE_OBSERVATION_FORMAT
        and observed.get("id") == spec.id
        and observed.get("after_operation") == spec.after_operation,
        "pre_crash_physical_observation_invalid",
    )
    for key in (
        "fingerprint_trace_model_sha256",
        "fingerprint_logical_graph_sha256",
    ):
        value = observed.get(key)
        _require(
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value),
            f"pre_crash_{key}_invalid",
        )
    capture_high_water = observed.get("rollout_capture_high_water")
    _require(
        type(capture_high_water) is int and capture_high_water >= spec.after_operation,
        "pre_crash_rollout_capture_high_water_invalid",
    )
    return observed


def _require_shadow_result(result: Any, *, through_seq: int) -> None:
    _require(result.divergence is None, "recovered_shadow_divergence")
    _require(result.checkpoint is not None, "recovered_shadow_checkpoint_missing")
    _require(result.receipt is not None, "recovered_shadow_receipt_missing")
    _require(
        result.checkpoint.through_seq == through_seq
        and result.receipt.through_seq == through_seq,
        "recovered_shadow_high_water_mismatch",
    )


async def _recover_nonprivacy(
    bundle: _Bundle,
    spec: CrashPointSpec,
    pre: Mapping[str, Any],
) -> dict[str, Any]:
    from okto_pulse.community.adapters.graph_rollout_coordinator import (
        BoardGraphRolloutRefused,
    )
    from okto_pulse.community.adapters.graph_rollout_journal import (
        CommunityGraphRolloutJournal,
    )

    board_id = bundle.context.board_id
    coordinator = bundle.board.graph_rollout_coordinator
    journal = CommunityGraphRolloutJournal(bundle.board.binding_store.root, board_id)
    crashed = journal.verify()
    binding = bundle.board.binding_store.acquire_board_binding(board_id)
    capture_high_water = int(pre["rollout_capture_high_water"])

    if spec.id in {
        "outbox-prepared-before-provider-call",
        "provider-return-before-outbox-terminal",
    }:
        _require(crashed.state == "shadowing", "prepared_recovery_state_invalid")
        _require(binding.backend == "ladybug", "prepared_recovery_binding_changed")
        _require(
            crashed.next_seq == capture_high_water + 2,
            "prepared_probe_not_durable",
        )
        recovered = coordinator.recover(board_id)
        _require(recovered.state == "shadowing", "prepared_recovery_not_shadowing")
        cycle = coordinator.run_shadow_cycle(board_id)
        _require_shadow_result(cycle, through_seq=capture_high_water + 1)
    elif spec.id in {
        "source-snapshot-close-before-candidate-open",
        "candidate-write-before-certificate",
    }:
        _require(crashed.state == "shadowing", "candidate_crash_state_invalid")
        _require(binding.backend == "ladybug", "candidate_crash_binding_changed")
        _require(crashed.candidate.binding_sha256 is None, "candidate_published_early")
        _require(
            crashed.candidate.physical_path.exists(), "candidate_crash_bytes_missing"
        )
        abandoned_generation = crashed.candidate.generation
        coordinator.recover(board_id)
        cycle = coordinator.run_shadow_cycle(board_id)
        _require_shadow_result(cycle, through_seq=capture_high_water)
        _require(
            cycle.rollout.candidate.generation != abandoned_generation,
            "candidate_generation_was_reused",
        )
        _require(
            crashed.candidate.physical_path.exists(), "candidate_crash_evidence_lost"
        )
    elif spec.id == "checkpoint-fsync-before-canary-ready":
        checkpoint = journal.read_checkpoint("shadow")
        receipt = journal.latest_comparison_receipt("shadow")
        _require(crashed.state == "shadowing", "checkpoint_recovery_state_invalid")
        _require(checkpoint is not None, "checkpoint_not_durable")
        _require(
            checkpoint.through_seq == capture_high_water,
            "checkpoint_boundary_invalid",
        )
        _require(receipt is None, "comparison_receipt_published_before_hook")
        abandoned_generation = crashed.candidate.generation
        coordinator.recover(board_id)
        cycle = coordinator.run_shadow_cycle(board_id)
        _require_shadow_result(cycle, through_seq=capture_high_water)
        _require(
            cycle.rollout.candidate.generation != abandoned_generation,
            "checkpoint_candidate_generation_was_reused",
        )
    elif spec.id == "final-delta-before-binding-cas":
        checkpoint = journal.read_checkpoint("shadow")
        receipt = journal.latest_comparison_receipt("shadow")
        _require(
            crashed.state == "shadowing" and binding.backend == "ladybug",
            "final_delta_did_not_keep_ladybug",
        )
        _require(
            checkpoint is not None
            and receipt is not None
            and checkpoint.through_seq == capture_high_water
            and receipt.through_seq == capture_high_water,
            "final_delta_certificate_not_durable",
        )
        recovered = coordinator.recover(board_id)
        _require(
            recovered.state == "shadowing"
            and bundle.board.binding_store.acquire_board_binding(board_id).backend
            == "ladybug",
            "final_delta_recovery_changed_active_backend",
        )
        retried = coordinator.run_shadow_cycle(board_id)
        _require_shadow_result(retried, through_seq=capture_high_water)
        _require(
            bundle.board.binding_store.acquire_board_binding(board_id).backend
            == "ladybug",
            "final_delta_retry_published_binding",
        )
    elif spec.id in {
        "binding-replace-before-directory-fsync",
        "binding-cas-before-rollout-transition",
    }:
        _require(crashed.state == "canary_ready", "binding_crash_state_not_canary")
        _require(binding.backend == "grafx", "persisted_binding_not_grafx")
        recovered = coordinator.recover(board_id)
        _require(
            recovered.state == "grafx_active_rollback_open",
            "binding_recovery_did_not_reconcile_rollout",
        )
    elif spec.id == "rollback-close-before-first-grafx-write":
        _require(
            crashed.state == "grafx_active_rollback_closed",
            "rollback_close_not_durable",
        )
        _require(binding.backend == "grafx", "rollback_close_binding_not_grafx")
        _require(
            crashed.next_seq == capture_high_water + 2,
            "rollback_close_probe_not_prepared",
        )
        recovered = coordinator.recover(board_id)
        _require(
            recovered.state == "grafx_active_rollback_closed",
            "rollback_close_reopened_during_recovery",
        )
        try:
            coordinator.rollback(board_id)
        except BoardGraphRolloutRefused as failure:
            _require(
                failure.details.get("reason") == "rollback_window_not_open",
                "rollback_close_refusal_reason_invalid",
            )
        else:
            raise CrashHarnessError("rollback_succeeded_after_durable_close")
    else:  # pragma: no cover - caller partitions privacy separately
        raise CrashHarnessError(f"nonprivacy_recovery_unmapped:{spec.id}")

    await bundle.board.graph_lifecycle.open(board_id)
    await _verify_all(bundle)
    observed = _observe(bundle)
    _require(
        observed["fingerprint_trace_model_sha256"]
        == pre["fingerprint_trace_model_sha256"],
        "recovered_trace_fingerprint_changed",
    )
    _require(
        observed["fingerprint_logical_graph_sha256"]
        == pre["fingerprint_logical_graph_sha256"],
        "recovered_logical_fingerprint_changed",
    )
    storage_identity, generation = _identity(bundle)
    return {
        "format": _RECOVERY_FORMAT,
        "observed_recovery": spec.expected_recovery,
        "recovered": True,
        "recovered_storage_identity": storage_identity,
        "recovered_generation": generation,
        "verify_ok": True,
        "verify_scope": "all",
        **observed,
        "absence_verified": False,
        "fingerprint_observation_phase": "post_recovery",
        "worker_pid": os.getpid(),
    }


def _aggregate_absence(bundle: _Bundle) -> None:
    from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

    from okto_pulse.community.adapters.grafx_board_storage import (
        grafx_board_privacy_scope,
        grafx_board_privacy_storage_present,
    )
    from okto_pulse.community.adapters.graph_rollout_journal import (
        CommunityGraphRolloutJournal,
    )

    board_id = bundle.context.board_id
    store = bundle.board.binding_store
    try:
        store.inspect_board_binding(board_id)
    except GraphCapabilityUnavailable as failure:
        _require(
            failure.details.get("reason") == "binding_missing", "privacy_binding_error"
        )
    else:
        raise CrashHarnessError("privacy_binding_still_present")
    ladybug = store.board_ladybug_path(board_id)
    _require(not ladybug.exists(), "privacy_ladybug_primary_present")
    _require(
        not ladybug.parent.exists()
        or not any(ladybug.parent.glob(ladybug.name + ".*")),
        "privacy_ladybug_sidecar_present",
    )
    board_root = store.root / "boards" / board_id
    scope = grafx_board_privacy_scope(board_id, board_root)
    _require(
        not grafx_board_privacy_storage_present(scope),
        "privacy_grafx_or_binding_storage_present",
    )
    journal = CommunityGraphRolloutJournal(store.root, board_id)
    _require(not journal.privacy_storage_present(), "privacy_rollout_storage_present")


async def _recover_privacy(
    bundle: _Bundle,
    spec: CrashPointSpec,
    pre: Mapping[str, Any],
) -> dict[str, Any]:
    from okto_pulse.community.adapters.graph_rollout_journal import (
        CommunityGraphRolloutJournal,
    )

    board_id = bundle.context.board_id
    journal = CommunityGraphRolloutJournal(bundle.board.binding_store.root, board_id)
    crashed = journal.verify()
    _require(crashed.state == "erased", "privacy_invalidation_not_durable")
    if spec.id == "privacy-invalidation-before-copy-sweep":
        _require(
            bundle.board.binding_store.acquire_board_binding(board_id).backend
            == "ladybug",
            "privacy_invalidation_swept_binding_early",
        )
        _require(
            crashed.candidate.physical_path.exists(), "privacy_candidate_swept_early"
        )
    else:
        _require(
            not crashed.candidate.physical_path.exists(),
            "privacy_grafx_copy_not_swept_before_receipt_hook",
        )

    result = bundle.board.graph_runtime_store.erase_board_graph(
        board_id,
        reason="mpulse7_crash_recovery",
    )
    _require(
        result.error_code is None and result.status in {"erased", "not_found"},
        "privacy_recovery_erase_failed",
    )
    _aggregate_absence(bundle)
    return {
        "format": _RECOVERY_FORMAT,
        "observed_recovery": spec.expected_recovery,
        "recovered": True,
        "recovered_storage_identity": "absent",
        "recovered_generation": "absent",
        "verify_ok": True,
        "verify_scope": "aggregate_absence",
        "fingerprint_trace_model_sha256": pre["fingerprint_trace_model_sha256"],
        "fingerprint_logical_graph_sha256": pre["fingerprint_logical_graph_sha256"],
        "absence_verified": True,
        "fingerprint_observation_phase": "pre_invalidation",
        "worker_pid": os.getpid(),
    }


async def _recovery_worker(config: Mapping[str, Any], spec: CrashPointSpec) -> None:
    execution_authority = _collect_process_authority_sha256(
        certification=bool(config["certification"])
    )
    _require(
        execution_authority == config["expected_execution_authority_sha256"],
        "recovery_worker_execution_authority_mismatch",
    )
    _require_hook_evidence(config, spec)
    pre = _pre_observation(config, spec)
    bundle = await _build_bundle(
        config,
        initialize_if_missing=False,
        allow_missing_binding=spec.fingerprint_observation_phase == "pre_invalidation",
    )
    _require(
        _collect_process_authority_sha256(certification=bool(config["certification"]))
        == config["expected_execution_authority_sha256"],
        "recovery_bundle_execution_authority_mismatch",
    )
    try:
        if spec.fingerprint_observation_phase == "pre_invalidation":
            result = await _recover_privacy(bundle, spec, pre)
        else:
            result = await _recover_nonprivacy(bundle, spec, pre)
        result["execution_authority_sha256"] = execution_authority
        await _close_bundle(bundle)
    except BaseException:
        try:
            await _close_bundle(bundle)
        except Exception as cleanup:  # noqa: BLE001 - preserve the recovery failure
            traceback.print_exception(cleanup, file=sys.stderr)
        raise
    _write_json_atomic(Path(str(config["recovery_result_path"])), result)


async def _worker(mode: str, config_path: Path) -> None:
    config, spec = _load_config(config_path)
    if mode == "crash":
        await _crash_worker(config, spec)
        raise CrashHarnessError("crash_worker_returned_without_hard_exit")
    if mode == "recovery":
        await _recovery_worker(config, spec)
        return
    raise CrashHarnessError("crash_worker_mode_invalid")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("crash", "recovery"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    asyncio.run(_worker(arguments.worker, arguments.config.resolve()))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess
    raise SystemExit(main())


__all__ = [
    "CRASH_POINT_SPECS",
    "CrashHarnessError",
    "CrashPointSpec",
    "ProcessResult",
    "run_crash_point",
]
