from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_mpulse7_acceptance as acceptance_runner
from mpulse7_gate_support import (
    DeterministicGraphModel,
    expand_trace,
    load_gate_manifest,
)
from run_mpulse7_acceptance import (
    CERTIFICATION_FACTORY_REFS,
    CERTIFICATION_MANIFEST_CANONICAL_SHA256,
    CERTIFICATION_MANIFEST_FILE_SHA256,
    CERTIFICATION_PULSE_CORPUS_FILE_SHA256,
    CERTIFICATION_PULSE_CORPUS_LOGICAL_SHA256,
    CORE_AUTHORITY_MODULE,
    CORE_CHECKOUT_ENV,
    RUNNER_SOURCE_PATH,
    GateBackendContext,
    GateFailure,
    IsolatedPulseCorpusResult,
    IsolatedQueryResult,
    _closed_callback_receipt,
    _module_checkout_authority,
    _normalize_pulse_corpus_callback,
    _require_certification_input_digests,
    _require_dependency_revision_authority,
    _require_supplement_parity,
    _run_pulse_corpus,
    _run_queries,
    _source_file_record,
    _validated_crash_evidence,
    _worker_execution_authority_sha256,
    canonical_json_bytes,
    canonical_sha256,
    main,
    resolve_factory,
    run_acceptance_gate,
    run_isolated_board_query,
    run_isolated_pulse_corpus_case,
    verify_frozen_inputs,
)

MANIFEST = ROOT / "tests" / "fixtures" / "m_pulse_7_acceptance_gate_v1.json"
CORPUS = ROOT.parent / "okto_grafx" / "tests" / "corpus" / "pulse_query_corpus_1_0.json"
FINAL_FINGERPRINT = "e6b7f3abafdff55f8e4167d012083eddf2106f6ec9de7347bccd5d7e41097344"
OPERATIONS = expand_trace(load_gate_manifest(MANIFEST))


def _prefix_fingerprints() -> dict[int, str]:
    boundaries = {
        point["after_operation"]
        for point in load_gate_manifest(MANIFEST)["crash_points"]["points"]
    }
    model = DeterministicGraphModel()
    result: dict[int, str] = {}
    for operation in OPERATIONS:
        model.apply(operation)
        if operation["sequence"] in boundaries:
            result[operation["sequence"]] = model.fingerprint_sha256()
    return result


PREFIX_FINGERPRINTS = _prefix_fingerprints()


@dataclass
class _FakeState:
    calls: list[str] = field(default_factory=list)
    commits: int = 0
    rollbacks: int = 0
    recoveries: list[tuple[int, int, str]] = field(default_factory=list)
    raw_ids: list[str] = field(default_factory=list)
    raw_entry_sha256s: list[str] = field(default_factory=list)
    scenario_ids: list[str] = field(default_factory=list)
    closes: int = 0
    factory_calls: int = 0
    model: DeterministicGraphModel = field(default_factory=DeterministicGraphModel)

    def apply_next(self, method: str) -> None:
        operation = OPERATIONS[len(self.calls)]
        assert operation["method"] == method
        self.model.apply(operation)
        self.calls.append(method)


_FAKE_STATES: dict[str, _FakeState] = {}


class _FakeStore:
    _MUTATIONS = frozenset(
        {
            "create_node",
            "create_edge",
            "update_node",
            "mark_superseded",
            "increment_attestation",
            "delete_edges_by_session",
            "delete_nodes_by_session",
        }
    )

    def __init__(self, state: _FakeState) -> None:
        self._state = state

    def __getattr__(self, name: str):
        if name not in self._MUTATIONS:
            raise AttributeError(name)

        def mutation(*_args: Any, **_kwargs: Any) -> None:
            self._state.apply_next(f"SemanticGraphStore.{name}")

        return mutation


class _FakeScope:
    _MUTATIONS = frozenset(
        {
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
        }
    )

    def __init__(self, state: _FakeState) -> None:
        self._state = state

    def __getattr__(self, name: str):
        if name not in self._MUTATIONS:
            raise AttributeError(name)

        def mutation(*_args: Any, **_kwargs: Any) -> None:
            prefix = (
                "GraphTransactionScopeExtension"
                if name == "replace_with_source_deleted_tombstone"
                else "GraphTransactionScope"
            )
            self._state.apply_next(f"{prefix}.{name}")

        return mutation

    async def commit(self) -> None:
        self._state.commits += 1

    async def rollback(self) -> None:
        self._state.rollbacks += 1


class _FakeTransaction:
    def __init__(self, state: _FakeState) -> None:
        self._state = state

    async def begin(self, _board_id: str) -> _FakeScope:
        return _FakeScope(self._state)


class _FakeBackend:
    def __init__(self, context: GateBackendContext, state: _FakeState) -> None:
        self._context = context
        self._state = state
        self.semantic_store = _FakeStore(state)
        self.graph_transaction = _FakeTransaction(state)

    def identity(self) -> dict[str, Any]:
        return {
            "backend": self._context.backend,
            "backend_version": f"fake-{self._context.backend}-1",
            "generation": "generation-1",
            "storage_identity": f"{self._context.run_id}/{self._context.backend}",
        }

    def observe_fingerprints(self) -> dict[str, str]:
        state = self._state.model.export_state()
        return {
            "logical_graph_sha256": canonical_sha256({"logical_graph": state}),
            "trace_model_sha256": self._state.model.fingerprint_sha256(),
        }

    def reopen_recover_verify_fingerprint(
        self,
        *,
        after_operations: int,
        verify_scope: str,
    ) -> dict[str, Any]:
        self._state.recoveries.append(
            (after_operations, len(self._state.calls), verify_scope)
        )
        self._state.model = self._state.model.recovered_copy()
        fingerprints = self.observe_fingerprints()
        identity = self.identity()
        return {
            "after_operations": after_operations,
            "closed": True,
            "fingerprint_logical_graph_sha256": fingerprints["logical_graph_sha256"],
            "fingerprint_trace_model_sha256": fingerprints["trace_model_sha256"],
            "generation": identity["generation"],
            "reopened": True,
            "recovered": True,
            "storage_identity": identity["storage_identity"],
            "verify_ok": True,
            "verify_scope": verify_scope,
        }

    def run_crash_point(self, point: dict[str, Any]) -> dict[str, Any]:
        privacy = point["hook"] in {
            "after_privacy_invalidation_fsync",
            "after_all_copy_sweeps",
        }
        return {
            "absence_verified": privacy,
            "after_operation": point["after_operation"],
            "crash_exit_code": 86,
            "crash_process_pid": os.getpid() + 1000,
            "expected_recovery": point["expected_recovery"],
            "fingerprint_logical_graph_sha256": canonical_sha256(
                {"crash-prefix": point["after_operation"]}
            ),
            "fingerprint_observation_phase": (
                "pre_invalidation" if privacy else "post_recovery"
            ),
            "fingerprint_trace_model_sha256": PREFIX_FINGERPRINTS[
                point["after_operation"]
            ],
            "hook": point["hook"],
            "id": point["id"],
            "observed_recovery": point["expected_recovery"],
            "recovered": True,
            "recovered_generation": "absent" if privacy else "crash-generation",
            "recovered_storage_identity": (
                "absent" if privacy else f"crash/{point['id']}"
            ),
            "recovery_process_pid": os.getpid(),
            "verify_ok": True,
            "verify_scope": "aggregate_absence" if privacy else "all",
        }

    def run_pulse_corpus_case(self, entry: dict[str, Any]) -> dict[str, Any]:
        if entry["class"] == "fragment":
            result: dict[str, Any] = {"outcome": "fragment"}
            status = "not_executable"
        elif entry["classification"] == "generic_gap":
            result = {
                "error_code": str(entry["expected"]["error"]["code"]),
                "error_type": str(entry["expected"]["error"]["type"]),
                "outcome": "error",
            }
            status = "executed"
        elif entry["class"] == "read":
            result = {"outcome": "rows", "rows": [[entry["id"]]]}
            status = "executed"
        else:
            result = {
                "effect": {"entry_id": entry["id"]},
                "outcome": "effect",
            }
            status = "executed"
        return {
            "class": entry["class"],
            "classification": entry["classification"],
            "id": entry["id"],
            "result": result,
            "status": status,
        }

    def run_raw_execute_family(self, entry: dict[str, Any]) -> dict[str, Any]:
        family_id = str(entry["id"])
        assert entry["class"] == "write"
        assert entry["classification"] == "already_supported"
        assert isinstance(entry["template"], str)
        self._state.raw_ids.append(family_id)
        self._state.raw_entry_sha256s.append(canonical_sha256(entry))
        return {
            "id": family_id,
            "result": {"family_id": family_id},
            "status": "passed",
        }

    def run_receipt_bound_scenario(self, scenario_id: str) -> dict[str, Any]:
        self._state.scenario_ids.append(scenario_id)
        return {
            "id": scenario_id,
            "result": {"scenario_id": scenario_id},
            "status": "passed",
        }

    def close(self) -> None:
        self._state.closes += 1


def _fake_factory(context: GateBackendContext) -> _FakeBackend:
    state = _FAKE_STATES.setdefault(context.backend, _FakeState())
    state.factory_calls += 1
    return _FakeBackend(context, state)


def _fake_query_runner(
    _factory: Any,
    context: GateBackendContext,
    case: dict[str, Any],
    timeout_seconds: int,
) -> IsolatedQueryResult:
    assert timeout_seconds == 30
    backend = _FakeBackend(context, _FAKE_STATES[context.backend])
    identity = backend.identity()
    fingerprints = backend.observe_fingerprints()
    result_sha256 = canonical_sha256(
        {"case_id": case["id"], "rows": [[case["method"]]]}
    )
    return IsolatedQueryResult(
        case_id=case["id"],
        fingerprint_logical_graph_sha256=fingerprints["logical_graph_sha256"],
        fingerprint_trace_model_sha256=fingerprints["trace_model_sha256"],
        generation=identity["generation"],
        ordering=case["ordering"],
        result_sha256=result_sha256,
        row_count=1,
        storage_identity=identity["storage_identity"],
        worker_pid=os.getpid() + 1,
    )


def _fake_pulse_query_runner(
    _factory: Any,
    context: GateBackendContext,
    entry: dict[str, Any],
    timeout_seconds: int,
) -> IsolatedPulseCorpusResult:
    assert timeout_seconds == 30
    backend = _FakeBackend(context, _FAKE_STATES[context.backend])
    identity = backend.identity()
    fingerprints = backend.observe_fingerprints()
    return IsolatedPulseCorpusResult(
        entry_class=entry["class"],
        entry_id=entry["id"],
        fingerprint_logical_graph_sha256=fingerprints["logical_graph_sha256"],
        fingerprint_trace_model_sha256=fingerprints["trace_model_sha256"],
        generation=identity["generation"],
        result_sha256=canonical_sha256({"entry_id": entry["id"]}),
        status="not_executable" if entry["class"] == "fragment" else "executed",
        storage_identity=identity["storage_identity"],
        worker_pid=os.getpid() + 1,
    )


class _WorkerStore:
    def get_schema_version(self, *, board_id: str) -> list[list[str]]:
        return [[board_id, "1"]]


class _WorkerBackend:
    def __init__(self, context: GateBackendContext) -> None:
        self._context = context
        self.semantic_store = _WorkerStore()
        self.graph_transaction = object()

    def identity(self) -> dict[str, str]:
        return {
            "backend": self._context.backend,
            "backend_version": "worker-1",
            "generation": "worker-generation",
            "storage_identity": "worker-storage",
        }

    def reopen_recover_verify_fingerprint(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("not used by the isolated query worker")

    def observe_fingerprints(self) -> dict[str, str]:
        return {
            "logical_graph_sha256": "a" * 64,
            "trace_model_sha256": FINAL_FINGERPRINT,
        }

    def run_crash_point(self, _point: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("not used by the isolated query worker")

    def run_pulse_corpus_case(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "class": entry["class"],
            "classification": entry["classification"],
            "id": entry["id"],
            "result": {"outcome": "rows", "rows": [[entry["id"]]]},
            "status": "executed",
        }

    def run_raw_execute_family(self, _entry: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("not used by the isolated query worker")

    def run_receipt_bound_scenario(self, _scenario_id: str) -> dict[str, Any]:
        raise AssertionError("not used by the isolated query worker")

    def close(self) -> None:
        return None


class _WorkerFactory:
    def __call__(self, context: GateBackendContext) -> _WorkerBackend:
        return _WorkerBackend(context)


class _NoOpStore(_FakeStore):
    def __getattr__(self, name: str):
        if name not in self._MUTATIONS:
            raise AttributeError(name)

        def mutation(*_args: Any, **_kwargs: Any) -> None:
            self._state.calls.append(f"SemanticGraphStore.{name}")

        return mutation


class _NoOpScope(_FakeScope):
    def __getattr__(self, name: str):
        if name not in self._MUTATIONS:
            raise AttributeError(name)

        def mutation(*_args: Any, **_kwargs: Any) -> None:
            prefix = (
                "GraphTransactionScopeExtension"
                if name == "replace_with_source_deleted_tombstone"
                else "GraphTransactionScope"
            )
            self._state.calls.append(f"{prefix}.{name}")

        return mutation


class _NoOpTransaction(_FakeTransaction):
    async def begin(self, _board_id: str) -> _NoOpScope:
        return _NoOpScope(self._state)


class _NoOpBackend(_FakeBackend):
    def __init__(self, context: GateBackendContext, state: _FakeState) -> None:
        super().__init__(context, state)
        self.semantic_store = _NoOpStore(state)
        self.graph_transaction = _NoOpTransaction(state)


_NOOP_STATES: dict[str, _FakeState] = {}


def _noop_factory(context: GateBackendContext) -> _NoOpBackend:
    state = _NOOP_STATES.setdefault(context.backend, _FakeState())
    state.factory_calls += 1
    return _NoOpBackend(context, state)


def test_frozen_inputs_authenticate_exact_gate_and_corpus() -> None:
    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)

    assert len(inputs.operations) == 10_000
    assert inputs.manifest["trace"]["expanded_trace_sha256"] == (
        "243d25a4fc807b5b29b63a64c597acf56d1dc94ca026030077178ba6abd86bea"
    )
    assert len(inputs.manifest["board_result_supplement"]["queries"]) == 19
    assert len(inputs.manifest["raw_execute_supplement"]["family_ids"]) == 21
    assert len(inputs.manifest["receipt_bound_scenarios"]) == 4
    assert inputs.manifest_file_sha256 == CERTIFICATION_MANIFEST_FILE_SHA256
    assert inputs.manifest_canonical_sha256 == CERTIFICATION_MANIFEST_CANONICAL_SHA256
    assert inputs.pulse_corpus_file_sha256 == CERTIFICATION_PULSE_CORPUS_FILE_SHA256
    assert inputs.pulse_corpus["digest"] == CERTIFICATION_PULSE_CORPUS_LOGICAL_SHA256

    certified = verify_frozen_inputs(
        MANIFEST,
        pulse_corpus_path=CORPUS,
        certification=True,
    )
    assert certified.manifest_path == MANIFEST.resolve()
    assert certified.pulse_corpus_path == CORPUS.resolve()


def test_certification_rejects_an_alternate_self_consistent_manifest(
    tmp_path: Path,
) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["scope"]["source_revisions"]["community"] = "f" * 40
    alternate = tmp_path / "alternate-manifest.json"
    alternate.write_text(
        json.dumps(document, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )

    self_consistent = verify_frozen_inputs(alternate, pulse_corpus_path=CORPUS)
    assert self_consistent.manifest["scope"]["source_revisions"]["community"] == (
        "f" * 40
    )
    with pytest.raises(GateFailure, match="manifest physical SHA-256"):
        _require_certification_input_digests(
            replace(self_consistent, manifest_path=MANIFEST.resolve())
        )
    with pytest.raises(GateFailure, match="exact frozen M-PULSE-7 manifest path"):
        verify_frozen_inputs(
            alternate,
            pulse_corpus_path=CORPUS,
            certification=True,
        )


def test_certification_rejects_fake_factories_before_execution(
    tmp_path: Path,
) -> None:
    _FAKE_STATES.clear()
    with pytest.raises(GateFailure, match="exact ladybug factory authority"):
        asyncio.run(
            run_acceptance_gate(
                factories={"ladybug": _fake_factory, "grafx": _fake_factory},
                workspace=tmp_path / "work",
                receipt_path=tmp_path / "receipt.json",
                manifest_path=MANIFEST,
                pulse_corpus_path=CORPUS,
                execution_mode="certification",
            )
        )
    assert _FAKE_STATES == {}
    assert not (tmp_path / "receipt.json").exists()


def test_certification_rejects_injected_query_runner_before_execution(
    tmp_path: Path,
) -> None:
    real_factories = {
        backend: resolve_factory(reference)
        for backend, reference in CERTIFICATION_FACTORY_REFS.items()
    }
    with pytest.raises(GateFailure, match="standard Board subprocess runner"):
        asyncio.run(
            run_acceptance_gate(
                factories=real_factories,
                workspace=tmp_path / "work",
                receipt_path=tmp_path / "receipt.json",
                manifest_path=MANIFEST,
                pulse_corpus_path=CORPUS,
                query_runner=_fake_query_runner,
                execution_mode="certification",
            )
        )
    assert not (tmp_path / "receipt.json").exists()


def test_certification_rejects_injected_pulse_runner_before_execution(
    tmp_path: Path,
) -> None:
    real_factories = {
        backend: resolve_factory(reference)
        for backend, reference in CERTIFICATION_FACTORY_REFS.items()
    }
    with pytest.raises(GateFailure, match="standard Pulse corpus subprocess runner"):
        asyncio.run(
            run_acceptance_gate(
                factories=real_factories,
                workspace=tmp_path / "work",
                receipt_path=tmp_path / "receipt.json",
                manifest_path=MANIFEST,
                pulse_corpus_path=CORPUS,
                pulse_corpus_runner=_fake_pulse_query_runner,
                execution_mode="certification",
            )
        )
    assert not (tmp_path / "receipt.json").exists()


def test_certification_source_must_be_the_expected_tools_file() -> None:
    with pytest.raises(GateFailure, match="outside the expected tools path"):
        _source_file_record(
            __file__,
            expected_path=RUNNER_SOURCE_PATH,
            label="runner",
        )


def test_dependency_authority_uses_exact_tracked_clean_checkouts() -> None:
    inputs = verify_frozen_inputs(
        MANIFEST,
        pulse_corpus_path=CORPUS,
        certification=True,
    )
    code = f"""
import json
import sys
sys.path.insert(0, {str(TOOLS)!r})
from run_mpulse7_acceptance import (
    CORE_AUTHORITY_MODULE,
    CORE_CHECKOUT_ENV,
    GRAFX_AUTHORITY_MODULE,
    GRAFX_CHECKOUT_ENV,
    _module_checkout_authority,
)
print(json.dumps({{
    "core": _module_checkout_authority(
        label="Core",
        module_name=CORE_AUTHORITY_MODULE,
        checkout_environment=CORE_CHECKOUT_ENV,
    ),
    "okto_grafx": _module_checkout_authority(
        label="okto_grafx",
        module_name=GRAFX_AUTHORITY_MODULE,
        checkout_environment=GRAFX_CHECKOUT_ENV,
    ),
}}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=dict(os.environ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    authority = json.loads(completed.stdout.strip().splitlines()[-1])
    core = authority["core"]
    grafx = authority["okto_grafx"]
    process_authority = {"dependency_checkouts": {"core": core, "okto_grafx": grafx}}

    assert core["tracked_clean"] is True
    assert core["module_origin"].startswith("src/")
    assert grafx["tracked_clean"] is True
    assert grafx["untracked_allowed"] is True
    assert grafx["module_origin"].startswith("src/")
    _require_dependency_revision_authority(
        process_authority,
        inputs.manifest["scope"]["source_revisions"],
    )

    with pytest.raises(GateFailure, match="Core HEAD differs"):
        _require_dependency_revision_authority(
            process_authority,
            {
                **inputs.manifest["scope"]["source_revisions"],
                "core": "f" * 40,
            },
        )


def test_dependency_authority_rejects_core_preloaded_before_runner() -> None:
    code = f"""
import sys
sys.path.insert(0, {str(ROOT / "tests")!r})
sys.path.insert(0, {str(TOOLS)!r})
import mpulse7_gate_support
from run_mpulse7_acceptance import (
    CORE_AUTHORITY_MODULE,
    CORE_CHECKOUT_ENV,
    GateFailure,
    _module_checkout_authority,
)
try:
    _module_checkout_authority(
        label="Core",
        module_name=CORE_AUTHORITY_MODULE,
        checkout_environment=CORE_CHECKOUT_ENV,
    )
except GateFailure as failure:
    if "loaded before import authority measurement" not in str(failure):
        raise
    print("core-preload-rejected")
else:
    raise AssertionError("preloaded Core unexpectedly acquired certification authority")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=dict(os.environ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "core-preload-rejected"


def test_dependency_authority_rejects_module_from_another_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CORE_CHECKOUT_ENV, str(ROOT))
    with pytest.raises(GateFailure, match="outside the exact checkout"):
        _module_checkout_authority(
            label="Core",
            module_name=CORE_AUTHORITY_MODULE,
            checkout_environment=CORE_CHECKOUT_ENV,
        )


def test_import_authority_rejects_divergent_source_restored_after_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "authority-repository"
    source_root = repository / "src"
    source_root.mkdir(parents=True)
    module_name = f"authority_probe_{os.getpid()}_{abs(hash(str(tmp_path)))}"
    module_path = source_root / f"{module_name}.py"
    pristine = b'def observed_value():\n    return "pristine"\n'
    divergent = b'def observed_value():\n    return "divergent"\n'
    module_path.write_bytes(pristine)

    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init")
    git("config", "core.autocrlf", "false")
    git("config", "user.email", "mpulse7@example.invalid")
    git("config", "user.name", "M-PULSE-7 Test")
    git("add", "--", f"src/{module_name}.py")
    git("commit", "-m", "authority baseline")

    monkeypatch.syspath_prepend(str(source_root))
    module_path.write_bytes(divergent)
    importlib.invalidate_caches()
    acceptance_runner._register_module_import_authority(module_name)
    imported = importlib.import_module(module_name)
    assert imported.observed_value() == "divergent"

    module_path.write_bytes(pristine)
    git("diff", "--quiet")
    checkout_environment = "MPULSE7_TEST_IMPORT_AUTHORITY_REPO"
    monkeypatch.setenv(checkout_environment, str(repository))
    try:
        with pytest.raises(GateFailure, match="imported source bytes differ"):
            _module_checkout_authority(
                label="restored probe",
                module_name=module_name,
                checkout_environment=checkout_environment,
            )
    finally:
        sys.modules.pop(module_name, None)
        acceptance_runner._OBSERVED_IMPORT_AUTHORITIES.pop(module_name, None)
        acceptance_runner._PENDING_IMPORT_AUTHORITY_NAMES.discard(module_name)


def test_transitive_import_authority_rejects_source_restored_after_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "transitive-authority-repository"
    source_root = repository / "src"
    source_root.mkdir(parents=True)
    unique = f"{os.getpid()}_{abs(hash(str(tmp_path)))}"
    dependency_name = f"transitive_dependency_{unique}"
    factory_name = f"transitive_factory_{unique}"
    dependency_path = source_root / f"{dependency_name}.py"
    factory_path = source_root / f"{factory_name}.py"
    pristine = b'OBSERVED = "pristine"\n'
    dependency_path.write_bytes(b'OBSERVED = "divergent"\n')
    factory_path.write_text(
        f"import {dependency_name}\n",
        encoding="utf-8",
    )
    checkout_label = f"transitive_probe_{unique}"
    acceptance_runner._register_import_authority_checkout(
        checkout_label,
        repository,
        source_roots=(source_root,),
    )
    monkeypatch.syspath_prepend(str(source_root))
    importlib.invalidate_caches()
    try:
        importlib.import_module(factory_name)
        dependency = importlib.import_module(dependency_name)
        assert dependency.OBSERVED == "divergent"
        origin_key = acceptance_runner._origin_key(dependency_path)
        assert origin_key in (acceptance_runner._OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN)
        assert dependency_name not in acceptance_runner._OBSERVED_IMPORT_AUTHORITIES

        dependency_path.write_bytes(pristine)
        with pytest.raises(GateFailure, match="imported source bytes differ"):
            acceptance_runner._validated_origin_import_authority(
                dependency_path,
                label="transitive restored probe",
            )
    finally:
        sys.modules.pop(factory_name, None)
        sys.modules.pop(dependency_name, None)
        acceptance_runner._IMPORT_AUTHORITY_CHECKOUTS.pop(checkout_label, None)
        acceptance_runner._OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN.pop(
            acceptance_runner._origin_key(dependency_path),
            None,
        )
        acceptance_runner._OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN.pop(
            acceptance_runner._origin_key(factory_path),
            None,
        )


def test_certification_worker_recomputes_supervisor_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_authority = {"authority_format": "test-authority/1"}
    expected_digest = canonical_sha256(process_authority)
    factory = resolve_factory(CERTIFICATION_FACTORY_REFS["ladybug"])
    context = GateBackendContext(
        backend="ladybug",
        board_id="board",
        workspace="workspace",
        run_id="run",
        certification_process_authority_sha256=expected_digest,
    )
    monkeypatch.setattr(
        acceptance_runner,
        "collect_certification_process_authority",
        lambda: process_authority,
    )

    assert _worker_execution_authority_sha256(factory, context) == expected_digest

    mismatched_context = replace(
        context,
        certification_process_authority_sha256="f" * 64,
    )
    with pytest.raises(GateFailure, match="differs from supervisor"):
        _worker_execution_authority_sha256(factory, mismatched_context)


@pytest.mark.parametrize(
    ("worker_name", "payload"),
    [
        (
            "_query_worker_async",
            load_gate_manifest(MANIFEST)["board_result_supplement"]["queries"][0],
        ),
        (
            "_pulse_corpus_worker_async",
            json.loads(CORPUS.read_text(encoding="utf-8"))["entries"][0],
        ),
    ],
)
def test_isolated_worker_measures_authority_before_factory_execution(
    worker_name: str,
    payload: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def measured_authority(_factory: Any, _context: GateBackendContext) -> None:
        events.append("authority")

    def forbidden_factory(_context: GateBackendContext) -> None:
        events.append("factory")
        assert events == ["authority", "factory"]
        raise RuntimeError("stop after ordering observation")

    monkeypatch.setattr(
        acceptance_runner,
        "_worker_execution_authority_sha256",
        measured_authority,
    )
    context = GateBackendContext(
        backend="ladybug",
        board_id="board",
        workspace=str(tmp_path),
        run_id="run",
    )
    output_path = tmp_path / f"{worker_name}.json"
    worker = getattr(acceptance_runner, worker_name)

    asyncio.run(worker(output_path, forbidden_factory, context, payload))

    assert events == ["authority", "factory"]
    assert json.loads(output_path.read_text(encoding="utf-8"))["worker_status"] == (
        "failed"
    )


def test_runner_executes_both_surfaces_recovery_and_closed_supplements(
    tmp_path: Path,
) -> None:
    _FAKE_STATES.clear()
    receipt_path = tmp_path / "receipt.json"
    receipt = asyncio.run(
        run_acceptance_gate(
            factories={"ladybug": _fake_factory, "grafx": _fake_factory},
            workspace=tmp_path / "work",
            receipt_path=receipt_path,
            manifest_path=MANIFEST,
            pulse_corpus_path=CORPUS,
            board_id="board-gate",
            run_id="fixed-run",
            query_runner=_fake_query_runner,
            pulse_corpus_runner=_fake_pulse_query_runner,
        )
    )

    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)
    expected_methods = [operation["method"] for operation in inputs.operations]
    transaction_count = sum(
        not method.startswith("SemanticGraphStore.") for method in expected_methods
    )
    raw_ids = inputs.manifest["raw_execute_supplement"]["family_ids"]
    entries_by_id = {entry["id"]: entry for entry in inputs.pulse_corpus["entries"]}
    scenario_ids = [value["id"] for value in inputs.manifest["receipt_bound_scenarios"]]

    for backend in ("ladybug", "grafx"):
        state = _FAKE_STATES[backend]
        assert state.calls == expected_methods
        assert state.commits == transaction_count
        assert state.rollbacks == 0
        assert state.recoveries == [
            (2500, 2500, "all"),
            (5000, 5000, "all"),
            (7500, 7500, "all"),
        ]
        assert state.raw_ids == raw_ids
        assert state.raw_entry_sha256s == [
            canonical_sha256(entries_by_id[family_id]) for family_id in raw_ids
        ]
        assert state.scenario_ids == scenario_ids
        expected_factory_calls = 3 if backend == "ladybug" else 2
        assert state.factory_calls == expected_factory_calls
        assert state.closes == expected_factory_calls

    assert receipt["acceptance"] == {
        "crash_point_failures": 0,
        "passed": False,
        "pulse_corpus_explained_divergences": 0,
        "pulse_corpus_unexplained_divergences": 0,
        "query_timeout_failures": 0,
        "test_only": True,
        "unexplained_divergences": 0,
        "verify_failures": 0,
    }
    assert receipt["execution_mode"] == "test_only"
    assert receipt["certification_authority"] is None
    assert len(receipt["crash_points"]) == 11
    assert len(receipt["board_result_comparisons"]) == 19
    assert len(receipt["pulse_query_corpus_comparisons"]) == 97
    assert all(
        comparison["results_equal"]
        for comparison in receipt["board_result_comparisons"]
    )
    assert [record["backend"] for record in receipt["backends"]] == [
        "ladybug",
        "grafx",
    ]
    assert all(
        set(record["metrics"])
        == {
            "throughput_ops_per_second",
            "latency_ms_p50",
            "latency_ms_p90",
            "latency_ms_p99",
            "peak_memory_bytes",
        }
        for record in receipt["backends"]
    )

    encoded = receipt_path.read_bytes()
    persisted = json.loads(encoded)
    assert encoded == canonical_json_bytes(persisted) + b"\n"
    declared_receipt_sha = persisted.pop("receipt_sha256")
    assert declared_receipt_sha == canonical_sha256(persisted)


def test_default_query_runner_uses_a_distinct_process(tmp_path: Path) -> None:
    context = GateBackendContext(
        backend="ladybug",
        board_id="isolated-board",
        workspace=str(tmp_path),
        run_id="isolated-run",
    )
    case = {
        "id": "schema-version",
        "ordering": "ordered",
        "method": "get_schema_version",
        "arguments": {"board_id": "${board_id}"},
    }

    result = run_isolated_board_query(_WorkerFactory(), context, case, 30)

    assert result.case_id == "schema-version"
    assert result.fingerprint_logical_graph_sha256 == "a" * 64
    assert result.fingerprint_trace_model_sha256 == FINAL_FINGERPRINT
    assert result.generation == "worker-generation"
    assert result.ordering == "ordered"
    assert result.row_count == 1
    assert result.storage_identity == "worker-storage"
    assert result.worker_pid != os.getpid()


def test_default_pulse_corpus_runner_uses_a_distinct_process(tmp_path: Path) -> None:
    context = GateBackendContext(
        backend="ladybug",
        board_id="isolated-board",
        workspace=str(tmp_path),
        run_id="isolated-run",
    )
    entry = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS).pulse_corpus[
        "entries"
    ][0]

    result = run_isolated_pulse_corpus_case(_WorkerFactory(), context, entry, 30)

    assert result.entry_id == "I01"
    assert result.entry_class == "read"
    assert result.status == "executed"
    assert result.storage_identity == "worker-storage"
    assert result.generation == "worker-generation"
    assert result.fingerprint_trace_model_sha256 == FINAL_FINGERPRINT
    assert result.worker_pid != os.getpid()


def test_noop_backend_cannot_echo_a_successful_recovery(tmp_path: Path) -> None:
    _NOOP_STATES.clear()
    receipt = tmp_path / "must-not-exist.json"

    with pytest.raises(GateFailure, match="recovery boundary 2500 diverged"):
        asyncio.run(
            run_acceptance_gate(
                factories={"ladybug": _noop_factory, "grafx": _noop_factory},
                workspace=tmp_path / "work",
                receipt_path=receipt,
                manifest_path=MANIFEST,
                pulse_corpus_path=CORPUS,
                run_id="no-op-run",
                query_runner=_fake_query_runner,
                pulse_corpus_runner=_fake_pulse_query_runner,
            )
        )

    assert not receipt.exists()


def test_pulse_callback_rejects_the_old_bare_passed_shape() -> None:
    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)
    entry = inputs.pulse_corpus["entries"][0]
    identity = {
        "backend": "ladybug",
        "backend_version": "test",
        "generation": "g1",
        "storage_identity": "s1",
    }
    fingerprints = {
        "logical_graph_sha256": "a" * 64,
        "trace_model_sha256": "b" * 64,
    }

    with pytest.raises(GateFailure, match="open evidence shape"):
        _normalize_pulse_corpus_callback(
            {"id": entry["id"], "passed": True},
            entry=entry,
            identity=identity,
            fingerprints=fingerprints,
        )


def test_generic_gap_rejects_an_arbitrary_typed_error() -> None:
    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)
    entry = next(
        value
        for value in inputs.pulse_corpus["entries"]
        if value["classification"] == "generic_gap"
    )
    identity = {
        "backend": "grafx",
        "backend_version": "test",
        "generation": "g1",
        "storage_identity": "s1",
    }
    fingerprints = {
        "logical_graph_sha256": "a" * 64,
        "trace_model_sha256": "b" * 64,
    }

    with pytest.raises(GateFailure, match="returned the wrong typed error"):
        _normalize_pulse_corpus_callback(
            {
                "class": entry["class"],
                "classification": entry["classification"],
                "id": entry["id"],
                "result": {
                    "error_code": "arbitrary_error",
                    "error_type": "MemoryError",
                    "outcome": "error",
                },
                "status": "executed",
            },
            entry=entry,
            identity=identity,
            fingerprints=fingerprints,
        )


def test_board_worker_receipt_cannot_switch_storage_generation() -> None:
    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)
    contexts = {
        backend: GateBackendContext(
            backend=backend,
            board_id="board",
            workspace="workspace",
            run_id="run",
        )
        for backend in ("ladybug", "grafx")
    }
    records = {
        backend: {
            "backend": backend,
            "final_fingerprint_logical_graph_sha256": "a" * 64,
            "final_fingerprint_trace_model_sha256": FINAL_FINGERPRINT,
            "identity": {
                "backend": backend,
                "backend_version": "test",
                "generation": "expected-generation",
                "storage_identity": f"expected/{backend}",
            },
        }
        for backend in ("ladybug", "grafx")
    }

    def wrong_storage_runner(
        _factory: Any,
        _context: GateBackendContext,
        case: dict[str, Any],
        _timeout: int,
    ) -> IsolatedQueryResult:
        return IsolatedQueryResult(
            case_id=case["id"],
            fingerprint_logical_graph_sha256="a" * 64,
            fingerprint_trace_model_sha256=FINAL_FINGERPRINT,
            generation="wrong-generation",
            ordering=case["ordering"],
            result_sha256="b" * 64,
            row_count=0,
            storage_identity="wrong/storage",
            worker_pid=os.getpid() + 1,
        )

    with pytest.raises(GateFailure, match="opened the wrong generation"):
        asyncio.run(
            _run_queries(
                inputs,
                {"ladybug": _fake_factory, "grafx": _fake_factory},
                contexts,
                wrong_storage_runner,
                records,
            )
        )


def test_certification_board_worker_authority_must_match_supervisor() -> None:
    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)
    expected_authority = "c" * 64
    contexts = {
        backend: GateBackendContext(
            backend=backend,
            board_id="board",
            workspace="workspace",
            run_id="run",
            certification_process_authority_sha256=expected_authority,
        )
        for backend in ("ladybug", "grafx")
    }
    records = {
        backend: {
            "backend": backend,
            "final_fingerprint_logical_graph_sha256": "a" * 64,
            "final_fingerprint_trace_model_sha256": FINAL_FINGERPRINT,
            "identity": {
                "backend": backend,
                "backend_version": "test",
                "generation": "generation",
                "storage_identity": f"storage/{backend}",
            },
        }
        for backend in ("ladybug", "grafx")
    }

    def wrong_authority_runner(
        _factory: Any,
        context: GateBackendContext,
        case: dict[str, Any],
        _timeout: int,
    ) -> IsolatedQueryResult:
        return IsolatedQueryResult(
            case_id=case["id"],
            fingerprint_logical_graph_sha256="a" * 64,
            fingerprint_trace_model_sha256=FINAL_FINGERPRINT,
            generation="generation",
            ordering=case["ordering"],
            result_sha256="b" * 64,
            row_count=0,
            storage_identity=f"storage/{context.backend}",
            worker_pid=os.getpid() + 1,
            execution_authority_sha256="d" * 64,
        )

    with pytest.raises(GateFailure, match="different source authority"):
        asyncio.run(
            _run_queries(
                inputs,
                {"ladybug": _fake_factory, "grafx": _fake_factory},
                contexts,
                wrong_authority_runner,
                records,
            )
        )


def test_certification_pulse_worker_authority_must_match_supervisor() -> None:
    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)
    expected_authority = "c" * 64
    contexts = {
        backend: GateBackendContext(
            backend=backend,
            board_id="board",
            workspace="workspace",
            run_id="run",
            certification_process_authority_sha256=expected_authority,
        )
        for backend in ("ladybug", "grafx")
    }
    records = {
        backend: {
            "backend": backend,
            "final_fingerprint_logical_graph_sha256": "a" * 64,
            "final_fingerprint_trace_model_sha256": FINAL_FINGERPRINT,
            "identity": {
                "backend": backend,
                "backend_version": "test",
                "generation": "generation",
                "storage_identity": f"storage/{backend}",
            },
        }
        for backend in ("ladybug", "grafx")
    }

    def wrong_authority_runner(
        _factory: Any,
        context: GateBackendContext,
        entry: dict[str, Any],
        _timeout: int,
    ) -> IsolatedPulseCorpusResult:
        return IsolatedPulseCorpusResult(
            entry_class=entry["class"],
            entry_id=entry["id"],
            fingerprint_logical_graph_sha256="a" * 64,
            fingerprint_trace_model_sha256=FINAL_FINGERPRINT,
            generation="generation",
            result_sha256="b" * 64,
            status="not_executable" if entry["class"] == "fragment" else "executed",
            storage_identity=f"storage/{context.backend}",
            worker_pid=os.getpid() + 1,
            execution_authority_sha256="d" * 64,
        )

    with pytest.raises(GateFailure, match="different source authority"):
        asyncio.run(
            _run_pulse_corpus(
                inputs,
                {"ladybug": _fake_factory, "grafx": _fake_factory},
                contexts,
                wrong_authority_runner,
                records,
            )
        )


def test_only_frozen_generic_gaps_are_explained_divergences() -> None:
    inputs = verify_frozen_inputs(MANIFEST, pulse_corpus_path=CORPUS)
    contexts = {
        backend: GateBackendContext(
            backend=backend,
            board_id="board",
            workspace="workspace",
            run_id="run",
        )
        for backend in ("ladybug", "grafx")
    }
    records = {
        backend: {
            "backend": backend,
            "final_fingerprint_logical_graph_sha256": "a" * 64,
            "final_fingerprint_trace_model_sha256": FINAL_FINGERPRINT,
            "identity": {
                "backend": backend,
                "backend_version": "test",
                "generation": "generation",
                "storage_identity": f"storage/{backend}",
            },
        }
        for backend in ("ladybug", "grafx")
    }

    def classified_runner(
        _factory: Any,
        context: GateBackendContext,
        entry: dict[str, Any],
        _timeout: int,
    ) -> IsolatedPulseCorpusResult:
        semantic_result = {"entry_id": entry["id"]}
        if entry["classification"] == "generic_gap":
            semantic_result["backend"] = context.backend
        return IsolatedPulseCorpusResult(
            entry_class=entry["class"],
            entry_id=entry["id"],
            fingerprint_logical_graph_sha256="a" * 64,
            fingerprint_trace_model_sha256=FINAL_FINGERPRINT,
            generation="generation",
            result_sha256=canonical_sha256(semantic_result),
            status=("not_executable" if entry["class"] == "fragment" else "executed"),
            storage_identity=f"storage/{context.backend}",
            worker_pid=os.getpid() + 1,
        )

    comparisons, unexplained, explained = asyncio.run(
        _run_pulse_corpus(
            inputs,
            {"ladybug": _fake_factory, "grafx": _fake_factory},
            contexts,
            classified_runner,
            records,
        )
    )

    assert len(comparisons) == 97
    assert unexplained == []
    assert len(explained) == 13
    assert {item["classification"] for item in explained} == {"generic_gap"}


def test_backend_specific_supplement_digest_fails_bilateral_parity() -> None:
    with pytest.raises(GateFailure, match="raw execute supplement results differ"):
        _require_supplement_parity(
            [
                {
                    "backend": "ladybug",
                    "raw_execute_families": [{"id": "I05", "receipt_sha256": "a" * 64}],
                    "receipt_bound_scenarios": [],
                },
                {
                    "backend": "grafx",
                    "raw_execute_families": [{"id": "I05", "receipt_sha256": "b" * 64}],
                    "receipt_bound_scenarios": [],
                },
            ]
        )


def test_crash_evidence_cannot_echo_the_wrong_recovery() -> None:
    manifest = load_gate_manifest(MANIFEST)
    point = manifest["crash_points"]["points"][0]
    context = GateBackendContext(
        backend="ladybug",
        board_id="board",
        workspace="workspace",
        run_id="run",
    )
    evidence = _FakeBackend(context, _FakeState()).run_crash_point(point)
    evidence["observed_recovery"] = "synthetic-success"

    with pytest.raises(GateFailure, match="did not observe its expected recovery"):
        _validated_crash_evidence(
            evidence,
            point=point,
            expected_trace_fingerprint=PREFIX_FINGERPRINTS[point["after_operation"]],
        )


def test_crash_and_recovery_children_must_report_supervisor_authority() -> None:
    manifest = load_gate_manifest(MANIFEST)
    point = manifest["crash_points"]["points"][0]
    context = GateBackendContext(
        backend="ladybug",
        board_id="board",
        workspace="workspace",
        run_id="run",
    )
    expected_authority = "c" * 64
    evidence = _FakeBackend(context, _FakeState()).run_crash_point(point)
    evidence.update(
        {
            "crash_execution_authority_sha256": expected_authority,
            "recovery_execution_authority_sha256": expected_authority,
        }
    )

    receipt = _validated_crash_evidence(
        evidence,
        point=point,
        expected_trace_fingerprint=PREFIX_FINGERPRINTS[point["after_operation"]],
        expected_execution_authority_sha256=expected_authority,
    )
    assert receipt["crash_execution_authority_sha256"] == expected_authority
    assert receipt["recovery_execution_authority_sha256"] == expected_authority

    evidence["recovery_execution_authority_sha256"] = "d" * 64
    with pytest.raises(GateFailure, match="different source authority"):
        _validated_crash_evidence(
            evidence,
            point=point,
            expected_trace_fingerprint=PREFIX_FINGERPRINTS[point["after_operation"]],
            expected_execution_authority_sha256=expected_authority,
        )


def test_closed_supplement_rejects_backend_specific_open_payload() -> None:
    with pytest.raises(GateFailure, match="open evidence shape"):
        _closed_callback_receipt(
            {
                "backend": "ladybug",
                "id": "I05",
                "passed": True,
            },
            expected_id="I05",
            callback_name="raw execute family",
        )


def test_manifest_digest_drift_fails_before_factories(tmp_path: Path) -> None:
    drifted = tmp_path / "drifted.json"
    text = MANIFEST.read_text(encoding="utf-8").replace(
        "243d25a4fc807b5b29b63a64c597acf56d1dc94ca026030077178ba6abd86bea",
        "1" * 64,
        1,
    )
    drifted.write_text(text, encoding="utf-8")

    with pytest.raises(GateFailure, match="expanded trace digest"):
        verify_frozen_inputs(drifted, pulse_corpus_path=CORPUS)


def test_cli_fails_closed_for_noncanonical_factory_references(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--workspace",
            str(tmp_path / "work"),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--ladybug-factory",
            "missing_gate_module:factory",
            "--grafx-factory",
            "missing_gate_module:factory",
        ]
    )

    assert exit_code == 2
    assert not (tmp_path / "receipt.json").exists()
    assert "FAILED" in capsys.readouterr().err


def test_cli_always_requests_certification_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}

    def fake_resolve(_reference: str):
        return lambda _context: None

    async def fake_gate(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {
            "acceptance": {"passed": True, "test_only": False},
            "receipt_sha256": "a" * 64,
        }

    monkeypatch.setattr(acceptance_runner, "resolve_factory", fake_resolve)
    monkeypatch.setattr(acceptance_runner, "run_acceptance_gate", fake_gate)
    exit_code = main(
        [
            "--workspace",
            str(tmp_path / "work"),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--ladybug-factory",
            CERTIFICATION_FACTORY_REFS["ladybug"],
            "--grafx-factory",
            CERTIFICATION_FACTORY_REFS["grafx"],
        ]
    )

    assert exit_code == 0
    assert observed["execution_mode"] == "certification"
    assert "PASSED" in capsys.readouterr().out
