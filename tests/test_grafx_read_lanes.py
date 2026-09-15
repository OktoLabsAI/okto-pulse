"""Scoped scheduling does not grant access or bypass native transaction guards."""
from contextlib import contextmanager, ExitStack
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from okto_grafx.errors import GrafxLeaseTimeout
from okto_pulse.community.adapters.grafx_read_lanes import GrafxReadLanes
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_graph_store import CommunityGrafxGraphStore


def test_prefers_idle_lane_after_peer_finishes_instead_of_round_robin_busy_lane():
    lanes = GrafxReadLanes(2)
    with lanes.reserve("board") as slow:
        with lanes.reserve("board") as fast:
            assert (slow, fast) == (0, 1)
        # Blind round-robin would now choose occupied lane 0.
        for _ in range(4):
            with lanes.reserve("board") as next_read:
                assert next_read == 1
    assert lanes._active == {}


def test_overload_is_balanced_without_waiting_or_allocating_extra_lanes():
    lanes = GrafxReadLanes(2)
    with ExitStack() as scope:
        selected = [scope.enter_context(lanes.reserve("board")) for _ in range(5)]
        assert selected == [0, 1, 0, 1, 0]
        assert lanes._active["board"] == [3, 2]
        with lanes.reserve("other") as other:
            assert other == 1 and lanes._active["other"] == [0, 1]
        assert "other" not in lanes._active
    assert lanes._active == {}


def test_parallel_reservations_do_not_hold_bookkeeping_lock_during_io():
    lanes = GrafxReadLanes(2)
    entered, release = Event(), Event()
    def slow():
        with lanes.reserve("board") as lane:
            entered.set()
            assert release.wait(3)
            return lane
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(slow)
        try:
            assert entered.wait(3)
            with lanes.reserve("board") as peer:
                assert peer == 1
        finally:
            release.set()
        assert task.result(timeout=3) == 0
    assert lanes._active == {}


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_invalid_lane_count_refused(bad):
    with pytest.raises(ValueError):
        GrafxReadLanes(bad)


@pytest.mark.parametrize("family", ["scalar", "pair", "batch", "store"])
@pytest.mark.parametrize("failure", [None, "timeout", "error"])
def test_scope_covers_query_and_cleanup_and_releases_before_retry(family, failure):
    lanes = GrafxReadLanes(2)
    selected = []
    active = []
    scopes = []
    def query(*_args, **_kwargs):
        assert len(active) == 1
        assert lanes._active["board"][active[0]] == 1
        if failure == "timeout" and len(selected) == 1:
            raise GrafxLeaseTimeout("injected busy participant")
        if failure == "error":
            raise RuntimeError("injected query failure")
        return SimpleNamespace(rows=((7,),), columns=("value",))
    @contextmanager
    def transaction(mode):
        assert mode == "read" and active
        try:
            yield SimpleNamespace(execute=query)
        finally:
            assert active  # transaction cleanup must precede reservation release
    @contextmanager
    def reserve(board):
        with lanes.reserve(board) as lane:
            assert not active
            selected.append(lane)
            active.append(lane)
            scopes.append("enter")
            try:
                yield SimpleNamespace(execute=query, transaction=transaction, begin=transaction)
            finally:
                active.pop()
                scopes.append("exit")
    def forbidden(_board):
        raise AssertionError("scope must own participant selection")
    executor = CommunityGrafxCypherExecutor(forbidden, read_database_scope=reserve)
    store = CommunityGrafxGraphStore(forbidden, lambda *_: None, read_database_scope=reserve)
    operations = {
        "scalar": lambda: executor.execute_read_only("board", "RETURN 7"),
        "pair": lambda: executor.execute_read_only_pair("board", "RETURN 7", "RETURN 7"),
        "batch": lambda: executor.execute_read_only_batch("board", [("RETURN 7", None, 1)]*2),
        "store": lambda: store._read("board", operation="test", callback=lambda r: query()),
    }
    if failure == "error":
        with pytest.raises(Exception, match="injected|failed"):
            operations[family]()
    else:
        operations[family]()
    assert selected == ([0, 1] if failure == "timeout" else [0])
    assert scopes == [item for _ in selected for item in ("enter", "exit")]
    assert not active and lanes._active == {}


def test_failed_open_releases_the_scheduling_charge():
    lanes = GrafxReadLanes(2)
    @contextmanager
    def reserve(board):
        with lanes.reserve(board):
            raise RuntimeError("admission refused")
            yield  # pragma: no cover
    executor = CommunityGrafxCypherExecutor(lambda _: None, read_database_scope=reserve)
    with pytest.raises(Exception, match="admission|failed"):
        executor.execute_read_only("board", "RETURN 1")
    assert not lanes._active


def test_composition_scope_passes_selected_lane_and_releases_on_resolution_failure():
    from okto_pulse.community.adapters.routed_board_graph_composition import _GrafxBoardAccess
    access = _GrafxBoardAccess(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), configured_page_size=8192, connect=None,
        read_pools=(SimpleNamespace(read_only=True), SimpleNamespace(read_only=True)))
    calls = []
    def resolve(board, *, _lane=None):
        calls.append((board, _lane))
        if board == "denied":
            raise RuntimeError("route refused")
        return _lane
    access.read_database = resolve
    with access.read_database_scope("board") as slow:
        with access.read_database_scope("board") as fast:
            assert (slow, fast) == (0, 1)
        with access.read_database_scope("board") as again:
            assert again == 1
    with pytest.raises(RuntimeError, match="route refused"):
        with access.read_database_scope("denied"):
            pytest.fail("refused admission cannot yield a handle")
    assert not access._read_lanes._active
    assert calls[:3] == [("board", 0), ("board", 1), ("board", 1)]


def test_base_exception_does_not_leak_board_state():
    lanes = GrafxReadLanes(2)
    with pytest.raises(KeyboardInterrupt):
        with lanes.reserve("board"):
            raise KeyboardInterrupt()
    assert not lanes._active


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,failure", [
    (operation, failure)
    for operation in ("current_version", "validate")
    for failure in (None, "open", "admission", "read", "interrupt", "cleanup")
] + [("validate", "validate")])
async def test_schema_reads_charge_lane_through_admission_and_cleanup(
    monkeypatch, operation, failure,
):
    from okto_pulse.community.adapters import grafx_graph_schema_manager as module

    lanes = GrafxReadLanes(2)
    database = SimpleNamespace(identity=SimpleNamespace(page_size=8192))
    selected = []
    events = []
    target = module.PULSE_GRAFX_SCHEMA_MANIFEST.schema_version

    def step(name):
        assert lanes._active["board"] == [1, 1]
        events.append(name)
        if failure == name:
            raise RuntimeError("injected schema refusal")

    @contextmanager
    def reserve(board):
        with lanes.reserve(board) as lane:
            selected.append(lane)
            step("open")
            try:
                yield database
            finally:
                step("cleanup")

    def admit(board, db):
        assert board == "board" and db is database
        step("admission")

    def read(db):
        assert db is database
        step("read")
        if failure == "interrupt":
            raise KeyboardInterrupt()
        return target

    def validate(db):
        assert db is database
        step("validate")

    def forbidden(*_args):
        raise AssertionError("metadata scope must own reader selection")

    monkeypatch.setattr(module, "read_current_grafx_schema_version", read)
    monkeypatch.setattr(module, "validate_current_grafx_schema", validate)
    manager = module.CommunityGrafxGraphSchemaManager(
        forbidden, forbidden, read_database_resolver=forbidden,
        read_database_scope=reserve, admission=admit,
    )
    with lanes.reserve("board") as busy:
        assert busy == 0
        if failure == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                await getattr(manager, operation)("board")
        elif failure and operation == "current_version":
            with pytest.raises(Exception, match="schema refusal|failed"):
                await manager.current_version("board")
        else:
            result = await getattr(manager, operation)("board")
            if operation == "validate":
                assert result.valid is (failure is None)
            else:
                assert result == target
        assert lanes._active["board"] == [1, 0]
    assert selected == [1]
    assert not lanes._active
    assert ("cleanup" in events) is (failure != "open")
    if failure == "admission":
        assert "read" not in events
