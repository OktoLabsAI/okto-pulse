from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import mpulse7_crash_harness as crash_harness
from mpulse7_crash_harness import (
    CRASH_POINT_SPECS,
    CrashHarnessError,
    ProcessResult,
    _closed_receipt,
    _collect_process_authority_sha256,
    _run_process,
    _scenario_root,
    _validate_point,
)

MANIFEST = ROOT / "tests" / "fixtures" / "m_pulse_7_acceptance_gate_v3.json"


def _manifest_points() -> list[dict[str, object]]:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return list(document["crash_points"]["points"])


def test_crash_harness_coordinates_are_exactly_the_frozen_manifest() -> None:
    assert [spec.manifest_point() for spec in CRASH_POINT_SPECS] == _manifest_points()
    assert len(CRASH_POINT_SPECS) == 11
    assert len({spec.id for spec in CRASH_POINT_SPECS}) == 11
    assert len({spec.hook for spec in CRASH_POINT_SPECS}) == 11


def test_all_frozen_points_use_existing_injected_or_wrappable_seams() -> None:
    assert all(spec.requires_production_hook is False for spec in CRASH_POINT_SPECS)
    assert all(spec.seam for spec in CRASH_POINT_SPECS)
    assert [spec.fingerprint_observation_phase for spec in CRASH_POINT_SPECS] == [
        *(["post_recovery"] * 9),
        "pre_invalidation",
        "pre_invalidation",
    ]


def test_point_validation_fails_closed_on_coordinate_drift() -> None:
    point = _manifest_points()[0]
    assert _validate_point(point).id == point["id"]

    altered = dict(point)
    altered["after_operation"] = int(altered["after_operation"]) + 1
    with pytest.raises(CrashHarnessError, match="crash_point_coordinate_drift"):
        _validate_point(altered)

    open_shape = {**point, "invented": True}
    with pytest.raises(CrashHarnessError, match="crash_point_shape_drift"):
        _validate_point(open_shape)


def test_process_runner_observes_real_hard_exit_and_distinct_recovery_pid(
    tmp_path: Path,
) -> None:
    crashed = _run_process(
        [sys.executable, "-c", "import os; os._exit(23)"],
        cwd=tmp_path,
    )
    recovered = _run_process([sys.executable, "-c", "pass"], cwd=tmp_path)

    assert crashed.exit_code == 23
    assert recovered.exit_code == 0
    assert crashed.pid > 0
    assert recovered.pid > 0
    assert crashed.pid != recovered.pid


def test_two_short_children_execute_under_the_same_observed_authority(
    tmp_path: Path,
) -> None:
    expected = _collect_process_authority_sha256(certification=False)
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(TOOLS)!r});"
        "from mpulse7_crash_harness import _collect_process_authority_sha256;"
        "print(_collect_process_authority_sha256(certification=False))"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    first = _run_process([sys.executable, "-c", code], cwd=tmp_path, env=environment)
    second = _run_process([sys.executable, "-c", code], cwd=tmp_path, env=environment)

    assert first.exit_code == second.exit_code == 0
    assert first.stdout.strip().splitlines()[-1] == expected
    assert second.stdout.strip().splitlines()[-1] == expected


def test_certifying_main_runner_is_reused_without_a_second_module_copy(
    tmp_path: Path,
) -> None:
    runner_path = TOOLS / "run_mpulse7_acceptance.py"
    code = """
import json
import pathlib
import sys

runner_path = pathlib.Path(__RUNNER_PATH__).resolve()
source = runner_path.read_bytes()
marker_line = (
    source.decode("utf-8").splitlines().index('if __name__ == "__main__":') + 1
)

def suppress_cli(frame, event, argument):
    if (
        event == "line"
        and frame.f_code.co_filename == str(runner_path)
        and frame.f_lineno == marker_line
    ):
        frame.f_globals["__name__"] = "__mpulse7_authority_probe__"
    return suppress_cli

sys.settrace(suppress_cli)
globals()["__file__"] = str(runner_path)
exec(compile(source, str(runner_path), "exec"), globals(), globals())
sys.settrace(None)
globals()["__name__"] = "__main__"

runner = sys.modules["__main__"]
sentinel = {"probe": "certifying-main-runner"}
runner.collect_certification_process_authority = lambda: sentinel
runner.canonical_sha256 = lambda value: "a" * 64 if value is sentinel else "b" * 64

import mpulse7_crash_harness as crash_harness

observed = crash_harness._collect_process_authority_sha256(certification=True)
runner_modules = {
    name: id(module)
    for name, module in sys.modules.items()
    if getattr(module, "__file__", None)
    and pathlib.Path(module.__file__).resolve() == runner_path
}
print(json.dumps({
    "distinct_runner_objects": len(set(runner_modules.values())),
    "named_second_copy": "run_mpulse7_acceptance" in runner_modules,
    "observed": observed,
    "resolved_main": crash_harness._certification_runner_module() is runner,
}, sort_keys=True))
""".replace("__RUNNER_PATH__", repr(str(runner_path)))
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)

    completed = _run_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
    )

    assert completed.exit_code == 0, completed.stderr
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert observed == {
        "distinct_runner_objects": 1,
        "named_second_copy": False,
        "observed": "a" * 64,
        "resolved_main": True,
    }


def test_certification_runner_resolution_rejects_distinct_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = crash_harness._certification_runner_module()
    duplicate = ModuleType("run_mpulse7_acceptance_duplicate")
    duplicate.__file__ = str(TOOLS / "run_mpulse7_acceptance.py")
    monkeypatch.setitem(sys.modules, duplicate.__name__, duplicate)

    with pytest.raises(CrashHarnessError, match="runner_authority_module_ambiguous"):
        crash_harness._certification_runner_module()
    assert runner is not duplicate


def test_crash_worker_measures_authority_before_building_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_mpulse7_acceptance as acceptance_runner

    authority = "c" * 64
    events: list[str] = []
    spec = CRASH_POINT_SPECS[0]

    def collect_authority(*, certification: bool) -> str:
        assert certification is True
        events.append("authority")
        return authority

    async def forbidden_bundle(*_args: object, **_kwargs: object) -> None:
        events.append("bundle")
        assert events == ["authority", "bundle"]
        raise _OrderingObserved

    monkeypatch.setattr(
        crash_harness,
        "_collect_process_authority_sha256",
        collect_authority,
    )
    monkeypatch.setattr(crash_harness, "_build_bundle", forbidden_bundle)
    monkeypatch.setattr(
        acceptance_runner,
        "verify_frozen_inputs",
        lambda _path: SimpleNamespace(
            manifest={"crash_points": {"points": [spec.manifest_point()]}}
        ),
    )
    config = {
        "certification": True,
        "expected_execution_authority_sha256": authority,
        "manifest_path": str(MANIFEST),
    }

    with pytest.raises(_OrderingObserved):
        asyncio.run(crash_harness._crash_worker(config, spec))
    assert events == ["authority", "bundle"]


def test_crash_worker_replays_frozen_cold_recovery_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import run_mpulse7_acceptance as acceptance_runner

    authority = "c" * 64
    spec = next(
        candidate
        for candidate in CRASH_POINT_SPECS
        if candidate.id == "privacy-invalidation-before-copy-sweep"
    )
    events: list[object] = []

    class Coordinator:
        def start(self, board_id: str) -> SimpleNamespace:
            events.append(("start", board_id))
            return SimpleNamespace(
                state="shadowing",
                candidate=SimpleNamespace(
                    physical_path=tmp_path / "candidate" / "graph"
                ),
            )

    def bundle(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            context=SimpleNamespace(board_id="board", run_id="run"),
            board=SimpleNamespace(graph_rollout_coordinator=Coordinator()),
        )

    initial = bundle("initial")
    after_2500 = bundle("after-2500")
    after_5000 = bundle("after-5000")
    after_7500 = bundle("after-7500")
    reopened = iter((after_2500, after_5000, after_7500))
    operations = tuple({"sequence": sequence} for sequence in range(1, 8207))
    checkpoints = tuple(
        {
            "after_operations": sequence,
            "model_fingerprint_sha256": character * 64,
        }
        for sequence, character in ((2500, "a"), (5000, "b"), (7500, "d"))
    )

    async def build(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return initial

    async def execute(
        current: SimpleNamespace,
        _context: object,
        operation: dict[str, int],
    ) -> None:
        if operation["sequence"] in {1, 2501, 5001, 7501, 8001}:
            events.append(("execute", operation["sequence"], current.name))

    async def recover(
        _config: object,
        current: SimpleNamespace,
        *,
        after_operations: int,
        expected_trace_fingerprint: str,
    ) -> SimpleNamespace:
        next_bundle = next(reopened)
        events.append(
            (
                "recover",
                after_operations,
                expected_trace_fingerprint,
                current.name,
                next_bundle.name,
            )
        )
        return next_bundle

    monkeypatch.setattr(
        crash_harness,
        "_collect_process_authority_sha256",
        lambda *, certification: authority,
    )
    monkeypatch.setattr(crash_harness, "_build_bundle", build)
    monkeypatch.setattr(crash_harness, "_reopen_at_recovery_boundary", recover)
    monkeypatch.setattr(
        crash_harness, "_rollout_capture_high_water", lambda _bundle: 10002
    )
    monkeypatch.setattr(
        crash_harness,
        "_observe",
        lambda current: events.append(("observe", current.name)) or {},
    )
    monkeypatch.setattr(crash_harness, "_write_json_atomic", lambda *_args: None)
    monkeypatch.setattr(
        crash_harness,
        "_arm_and_crash",
        lambda current, *_args: (_ for _ in ()).throw(_OrderingObserved(current.name)),
    )
    monkeypatch.setattr(
        acceptance_runner,
        "verify_frozen_inputs",
        lambda _path: SimpleNamespace(
            manifest={
                "crash_points": {"points": [spec.manifest_point()]},
                "reopen_recovery_cycles": {"after_operations": [2500, 5000, 7500]},
                "trace": {"checkpoints": checkpoints},
            },
            operations=operations,
        ),
    )
    monkeypatch.setattr(acceptance_runner, "_execute_operation", execute)
    config = {
        "certification": True,
        "expected_execution_authority_sha256": authority,
        "manifest_path": str(MANIFEST),
        "pre_observation_path": str(tmp_path / "pre.json"),
    }

    with pytest.raises(_OrderingObserved, match="after-7500"):
        asyncio.run(crash_harness._crash_worker(config, spec))

    assert events == [
        ("start", "board"),
        ("execute", 1, "initial"),
        ("recover", 2500, "a" * 64, "initial", "after-2500"),
        ("execute", 2501, "after-2500"),
        ("recover", 5000, "b" * 64, "after-2500", "after-5000"),
        ("execute", 5001, "after-5000"),
        ("recover", 7500, "d" * 64, "after-5000", "after-7500"),
        ("execute", 7501, "after-7500"),
        ("execute", 8001, "after-7500"),
        ("observe", "after-7500"),
    ]


def test_replay_capture_high_water_accounts_for_frozen_recovery_boundaries() -> None:
    boundaries = frozenset((2500, 5000, 7500))

    assert (
        crash_harness._expected_replay_capture_high_water(
            after_operation=137,
            recovery_boundaries=boundaries,
        )
        == 137
    )
    assert (
        crash_harness._expected_replay_capture_high_water(
            after_operation=2500,
            recovery_boundaries=boundaries,
        )
        == 2501
    )
    assert (
        crash_harness._expected_replay_capture_high_water(
            after_operation=5001,
            recovery_boundaries=boundaries,
        )
        == 5003
    )
    assert (
        crash_harness._expected_replay_capture_high_water(
            after_operation=8001,
            recovery_boundaries=boundaries,
        )
        == 8004
    )


def test_recovery_boundary_closes_rebuilds_recovers_verifies_and_observes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = "c" * 64
    fingerprint = "a" * 64
    events: list[str] = []

    old = SimpleNamespace(context=SimpleNamespace(board_id="board"))
    recovery = SimpleNamespace(status="recovered", main_untouched=True)

    class Recovery:
        async def recover_wal_only(self, board_id: str) -> object:
            assert board_id == "board"
            events.append("recover")
            return recovery

    class Lifecycle:
        async def open(self, board_id: str) -> object:
            assert board_id == "board"
            events.append("open")
            return SimpleNamespace(opened=True)

    reopened = SimpleNamespace(
        context=SimpleNamespace(board_id="board"),
        board=SimpleNamespace(graph_recovery=Recovery(), graph_lifecycle=Lifecycle()),
    )

    async def close(current: object) -> None:
        assert current is old
        events.append("close")

    async def build(*_args: object, **kwargs: object) -> object:
        assert kwargs == {"initialize_if_missing": False}
        events.append("build")
        return reopened

    async def verify(current: object) -> None:
        assert current is reopened
        events.append("verify")

    def identity(current: object) -> tuple[str, str]:
        events.append("identity-old" if current is old else "identity-new")
        return ("storage", "generation")

    monkeypatch.setattr(crash_harness, "_close_bundle", close)
    monkeypatch.setattr(crash_harness, "_build_bundle", build)
    monkeypatch.setattr(crash_harness, "_verify_all", verify)
    monkeypatch.setattr(crash_harness, "_identity", identity)
    monkeypatch.setattr(
        crash_harness,
        "_observe",
        lambda current: (
            events.append("observe") or {"fingerprint_trace_model_sha256": fingerprint}
        ),
    )
    monkeypatch.setattr(
        crash_harness,
        "_collect_process_authority_sha256",
        lambda *, certification: events.append("authority") or authority,
    )
    config = {
        "certification": True,
        "expected_execution_authority_sha256": authority,
    }

    result = asyncio.run(
        crash_harness._reopen_at_recovery_boundary(
            config,
            old,
            after_operations=2500,
            expected_trace_fingerprint=fingerprint,
        )
    )

    assert result is reopened
    assert events == [
        "identity-old",
        "close",
        "build",
        "authority",
        "recover",
        "open",
        "verify",
        "identity-new",
        "observe",
    ]


def test_recovery_worker_measures_authority_before_building_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = "c" * 64
    events: list[str] = []
    spec = CRASH_POINT_SPECS[0]

    def collect_authority(*, certification: bool) -> str:
        assert certification is True
        events.append("authority")
        return authority

    async def forbidden_bundle(*_args: object, **_kwargs: object) -> None:
        events.append("bundle")
        assert events == ["authority", "bundle"]
        raise _OrderingObserved

    monkeypatch.setattr(
        crash_harness,
        "_collect_process_authority_sha256",
        collect_authority,
    )
    monkeypatch.setattr(crash_harness, "_build_bundle", forbidden_bundle)
    monkeypatch.setattr(crash_harness, "_require_hook_evidence", lambda *_args: {})
    monkeypatch.setattr(crash_harness, "_pre_observation", lambda *_args: {})
    config = {
        "certification": True,
        "expected_execution_authority_sha256": authority,
    }

    with pytest.raises(_OrderingObserved):
        asyncio.run(crash_harness._recovery_worker(config, spec))
    assert events == ["authority", "bundle"]


class _OrderingObserved(RuntimeError):
    pass


def test_scenario_root_is_short_and_contained_in_workspace(tmp_path: Path) -> None:
    backend = SimpleNamespace(
        _context=SimpleNamespace(workspace=str(tmp_path), run_id="gate-run")
    )
    root = _scenario_root(backend, CRASH_POINT_SPECS[0])

    assert root.parent == tmp_path / ".mp7" / "c"
    assert len(root.name) == 24
    assert root.relative_to(tmp_path)
    assert len(str(root / "kg" / "boards" / "board" / "grafx" / ("g" * 40))) < 260


def test_closed_receipt_has_only_the_runner_contract() -> None:
    spec = CRASH_POINT_SPECS[0]
    recovered = {
        "format": "okto-pulse-community-m-pulse-7-crash-recovery/1",
        "observed_recovery": spec.expected_recovery,
        "recovered": True,
        "recovered_storage_identity": "storage-id",
        "recovered_generation": "generation-id",
        "verify_ok": True,
        "verify_scope": "all",
        "fingerprint_trace_model_sha256": "a" * 64,
        "fingerprint_logical_graph_sha256": "b" * 64,
        "absence_verified": False,
        "fingerprint_observation_phase": "post_recovery",
        "worker_pid": 202,
        "execution_authority_sha256": "c" * 64,
    }
    receipt = _closed_receipt(
        spec=spec,
        crash=ProcessResult(101, 86, "", ""),
        recovery=ProcessResult(102, 0, "", ""),
        hook_evidence={"pid": 201, "execution_authority_sha256": "c" * 64},
        recovered=recovered,
        certification=False,
    )

    assert set(receipt) == {
        "absence_verified",
        "after_operation",
        "crash_exit_code",
        "crash_process_pid",
        "expected_recovery",
        "fingerprint_logical_graph_sha256",
        "fingerprint_observation_phase",
        "fingerprint_trace_model_sha256",
        "hook",
        "id",
        "observed_recovery",
        "recovered",
        "recovered_generation",
        "recovered_storage_identity",
        "recovery_process_pid",
        "verify_ok",
        "verify_scope",
    }
    assert receipt["crash_process_pid"] == 201
    assert receipt["recovery_process_pid"] == 202
    assert receipt["crash_exit_code"] == 86


def test_certification_receipt_exposes_both_child_authority_digests() -> None:
    spec = CRASH_POINT_SPECS[0]
    authority = "c" * 64
    recovered = {
        "format": "okto-pulse-community-m-pulse-7-crash-recovery/1",
        "observed_recovery": spec.expected_recovery,
        "recovered": True,
        "recovered_storage_identity": "storage-id",
        "recovered_generation": "generation-id",
        "verify_ok": True,
        "verify_scope": "all",
        "fingerprint_trace_model_sha256": "a" * 64,
        "fingerprint_logical_graph_sha256": "b" * 64,
        "absence_verified": False,
        "fingerprint_observation_phase": "post_recovery",
        "worker_pid": 202,
        "execution_authority_sha256": authority,
    }
    receipt = _closed_receipt(
        spec=spec,
        crash=ProcessResult(101, 86, "", ""),
        recovery=ProcessResult(102, 0, "", ""),
        hook_evidence={"pid": 201, "execution_authority_sha256": authority},
        recovered=recovered,
        certification=True,
    )

    assert receipt["crash_execution_authority_sha256"] == authority
    assert receipt["recovery_execution_authority_sha256"] == authority


def test_after_operation_is_prefix_and_rollback_probe_is_outside_trace() -> None:
    rollback = next(
        spec
        for spec in CRASH_POINT_SPECS
        if spec.id == "rollback-close-before-first-grafx-write"
    )
    assert rollback.after_operation == 8001
    assert rollback.seam.endswith("prepare_mutation:after_return")
    # Productive prepare_mutation commits rollback_closed and the extra prepared
    # row in one BEGIN IMMEDIATE transaction. Its return is therefore the first
    # observable durable boundary before the reserved no-op reaches Grafx.
    assert rollback.fingerprint_observation_phase == "post_recovery"
