"""Revision/serialization tests for tools/profile_m7_families.py (harness revision h1-h8).

No forensic board, no m7-cert-* workspace and no benchmark is touched: the tests exercise the
hook/timer plumbing on fakes, the CLI surface, the harness identity record and -- the important
one -- that the default fixture plan still hashes to the certified operation_set_sha256.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import profile_m7_families as harness  # noqa: E402

HARNESS_FILE = TOOLS / "profile_m7_families.py"
CERTIFIED_SOURCE_SHA256 = (
    "0cb60d5e43e29ac82e9a41534668c2237b7be78534fca789b4f37c4601862591"
)
# operation_set_sha256 of every pf5 report (scope path, per_family=5, harness node types).
CERTIFIED_PF5_OPERATION_SET_SHA256 = (
    "c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81"
)


def test_help_lists_the_h1_h8_flags() -> None:
    completed = subprocess.run(
        [sys.executable, "-B", str(HARNESS_FILE), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for flag in (
        "--delete-nodes-types",
        "--machine-idle-asserted",
        "--no-phases",
        "--community",
        "--scratch",
    ):
        assert flag in completed.stdout


def test_harness_revision_identifies_this_file() -> None:
    revision = harness.harness_revision()
    assert revision["name"] == "h1-h8"
    assert revision["sha256"] == hashlib.sha256(HARNESS_FILE.read_bytes()).hexdigest()
    assert revision["certified_source_sha256"] == CERTIFIED_SOURCE_SHA256
    assert "comparable ONLY" in revision["instrumented_comparability"]


def test_hook_target_lists_cover_h2_h7_h8() -> None:
    engine = {(cls, method) for _, cls, method in harness.Hooks.ENGINE_TARGETS}
    for expected in (
        ("Database", "_run_statement"),
        ("QueryEngine", "execute"),
        ("QueryEngine", "_planned_for"),
        ("QueryEngine", "planned"),
        ("LocalProcessCoordinator", "_take_file_lock"),
        ("LocalProcessCoordinator", "_wait_for_os_lock"),
        ("LocalProcessCoordinator", "_drop_lock"),
        ("BufferPool", "read_fresh_page"),
        ("BufferPool", "_read_page"),
        ("BufferPool", "_invalidate"),
    ):
        assert expected in engine
    functions = {(module, name) for module, name in harness.Hooks.MODULE_FUNCTIONS}
    assert ("okto_grafx.adapters.storage_local", "_open_descriptor") in functions
    assert ("okto_grafx.engine.public_views", "_query_parameters_snapshot") in functions
    assert ("okto_grafx.engine.public_views", "_query_result_view") in functions
    assert "close" in harness.Hooks.OS_NAMES
    assert ("msvcrt", "locking") in harness.Hooks.WINDOWS_MODULE_FUNCTIONS
    assert (
        "TransactionManager.page_access_section"
        in harness.Hooks.EXCLUDED_CONTEXT_MANAGERS
    )


def test_wrap_counts_calls_and_inclusive_time_then_restores() -> None:
    class Owner:
        def work(self, n: int) -> int:
            return n * 2

    original = Owner.work
    hooks = harness.Hooks("grafx")
    hooks._wrap(Owner, "work", "Owner.work")
    assert Owner().work(21) == 42
    assert Owner().work(1) == 2
    snapshot = hooks.snapshot()
    assert snapshot["calls"]["Owner.work"] == 2
    assert snapshot["inclusive_ms"]["Owner.work"] >= 0.0
    assert snapshot["unmeasurable_targets"] == []
    assert snapshot["attribution_note"] == harness.Hooks.ATTRIBUTION_NOTE
    hooks.uninstall()
    assert Owner.work is original


def test_generator_targets_are_flagged_unmeasurable() -> None:
    class Owner:
        def stream(self):
            yield 1

    hooks = harness.Hooks("grafx")
    hooks._wrap(Owner, "stream", "Owner.stream")
    assert list(Owner().stream()) == [1]
    assert hooks.snapshot()["unmeasurable_targets"] == ["Owner.stream"]
    assert hooks.snapshot()["calls"]["Owner.stream"] == 1
    hooks.uninstall()


def test_invalidate_wrapper_classifies_read_view_drops() -> None:
    class Pool:
        def _invalidate(self, file, *, doom_pinned):
            return (file, doom_pinned)

    hooks = harness.Hooks("grafx")
    hooks._wrap_invalidate(Pool)
    pool = Pool()
    assert pool._invalidate(None, doom_pinned=True) == (None, True)
    assert pool._invalidate("heap.dat", doom_pinned=False) == ("heap.dat", False)
    assert pool._invalidate("index/a.idx", doom_pinned=False) == ("index/a.idx", False)
    snapshot = hooks.snapshot()
    assert snapshot["calls"]["BufferPool._invalidate"] == 3
    assert snapshot["read_view_drops"] == {"all": 1, "file": 2, "doom_pinned": 1}
    hooks.reset()
    assert hooks.snapshot()["read_view_drops"] == {
        "all": 0,
        "file": 0,
        "doom_pinned": 0,
    }
    hooks.uninstall()


class _FakeScope:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, params: dict | None = None) -> SimpleNamespace:
        self.statements.append(statement)
        return SimpleNamespace(rows=[])

    async def commit(self) -> str:
        return "committed"

    async def rollback(self) -> str:
        return "rolled back"


class _FakeTransactions:
    async def begin(self, board_id: str) -> _FakeScope:
        return _FakeScope()


async def test_phase_timers_time_begin_execute_and_commit_on_the_instance() -> None:
    backend = SimpleNamespace(graph_transaction=_FakeTransactions())
    original_begin = backend.graph_transaction.begin
    timers = harness.PhaseTimers()
    timers.attach(backend)
    assert timers.available, timers.reason
    scope = await backend.graph_transaction.begin("board")
    scope.execute("MATCH (n) RETURN n")
    scope.execute("CREATE (n)")
    assert await scope.commit() == "committed"
    snapshot = timers.snapshot()
    assert snapshot["available"] is True
    assert snapshot["begins"] == 1
    assert snapshot["execute_count"] == 2
    assert len(snapshot["execute_ms"]) == 2
    assert snapshot["commit_ms"] >= 0.0
    assert snapshot["rollback_ms"] == 0.0
    assert scope.statements == ["MATCH (n) RETURN n", "CREATE (n)"]
    timers.reset()
    assert timers.snapshot()["begins"] == 0
    timers.detach()
    assert backend.graph_transaction.begin == original_begin


async def test_phase_timers_report_unavailable_instead_of_failing() -> None:
    class Frozen:
        __slots__ = ("begin",)

        def __init__(self) -> None:
            object.__setattr__(self, "begin", None)

        def __setattr__(self, name: str, value: object) -> None:
            raise AttributeError("frozen")

    backend = SimpleNamespace(graph_transaction=Frozen())
    timers = harness.PhaseTimers()
    timers.attach(backend)
    assert timers.available is False
    assert "AttributeError" in (timers.reason or "")
    assert timers.snapshot()["available"] is False


def test_fixtures_default_and_gate_delete_node_types() -> None:
    default = harness.Fixtures({}, 1, "scope")
    op, post = default.measured("delete_nodes_by_session", 0)
    assert op["payload"]["node_types"] == ["Decision", "Entity"]
    assert post["kind"] == "nodes_in_session"
    gate = harness.Fixtures(
        {}, 1, "scope", delete_node_types=("Decision", "Entity", "Bug")
    )
    op, _ = gate.measured("delete_nodes_by_session", 0)
    assert op["payload"]["node_types"] == ["Decision", "Entity", "Bug"]


def test_machine_state_has_the_h5_schema() -> None:
    state = harness._machine_state()
    assert state["method"] in {"psutil", "typeperf", "unavailable"}
    assert "sampled_at_utc" in state
    assert "cpu_percent" in state
    assert "python_processes" in state


@pytest.fixture(scope="module")
def runner():
    runner, _backends = harness._load_gate_modules()
    return runner


def test_gate_node_types_match_the_frozen_manifest(runner) -> None:
    types = harness._gate_node_types(runner)
    from okto_pulse.core.kg.schema_contract import NODE_TYPES

    assert types == list(NODE_TYPES)
    assert len(types) == 11


def test_default_plan_digest_is_the_certified_pf5_digest(runner) -> None:
    """Criterion 2: the RAW operation set is unchanged (scope path, per_family=5, harness node types)."""
    templates = harness._templates(runner)
    fixtures = harness.Fixtures(templates, 5, "scope")
    families = [f for f in harness.FAMILIES if True]
    plan = {f: [fixtures.measured(f, i) for i in range(5)] for f in families}
    assert (
        harness._plan_digest(plan, families, "scope")
        == CERTIFIED_PF5_OPERATION_SET_SHA256
    )


def test_gate_node_types_change_the_digest_by_design(runner) -> None:
    templates = harness._templates(runner)
    fixtures = harness.Fixtures(
        templates, 5, "scope", delete_node_types=tuple(harness._gate_node_types(runner))
    )
    families = list(harness.FAMILIES)
    plan = {f: [fixtures.measured(f, i) for i in range(5)] for f in families}
    assert (
        harness._plan_digest(plan, families, "scope")
        != CERTIFIED_PF5_OPERATION_SET_SHA256
    )
