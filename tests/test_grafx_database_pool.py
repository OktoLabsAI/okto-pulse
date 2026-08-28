"""What the Grafx pool must refuse, and what it must never lose.

The interesting cases are the negative ones: a handle that failed admission must
not be cached, a close that failed must stay reachable, and a junction anywhere
on the path must be refused before anything is opened -- on Python 3.11, where
pathlib alone reports a junction as an ordinary directory.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
)

from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabaseLease,
    GrafxDatabasePoolError,
)

PAGE_SIZE = 8192


class _FakeIdentity:
    def __init__(self, page_size: int) -> None:
        self.page_size = page_size


class _FakeDatabase:
    """A stand-in that reports the geometry and path admission checks."""

    def __init__(self, path: Path, page_size: int) -> None:
        self.path = str(path)
        self.identity = _FakeIdentity(page_size)
        self.close_calls = 0
        self.close_failures = 0
        self.closed = False

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures > 0:
            self.close_failures -= 1
            raise OSError("injected close failure")
        self.closed = True


class _Connector:
    """Records every open so a test can prove how many actually happened."""

    def __init__(self, *, page_size: int = PAGE_SIZE) -> None:
        self.page_size = page_size
        self.calls: list[tuple[Path, int]] = []
        self.databases: list[_FakeDatabase] = []
        self.failure: Exception | None = None
        # A real open takes time. Widening that window is what turns the
        # concurrency test into a race the pool can actually lose, instead of
        # one the GIL wins for it by finishing before the next thread starts.
        self.open_delay_seconds = 0.0

    def __call__(self, path: Path, *, page_size: int) -> _FakeDatabase:
        self.calls.append((Path(path), page_size))
        if self.open_delay_seconds:
            time.sleep(self.open_delay_seconds)
        if self.failure is not None:
            raise self.failure
        database = _FakeDatabase(Path(path), self.page_size)
        self.databases.append(database)
        return database


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "kg"
    base.mkdir()
    return base


def _pool(root: Path, connector: _Connector) -> CommunityGrafxDatabasePool:
    return CommunityGrafxDatabasePool(root, connect=connector)


def _make_junction(link: Path, target: Path) -> bool:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and link.exists()


class TestOneHandlePerDatabase:
    def test_the_same_path_is_opened_once_and_shared(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        first = pool.get(root / "board-a", page_size=PAGE_SIZE)
        second = pool.get(root / "board-a", page_size=PAGE_SIZE)

        assert first is second
        assert len(connector.calls) == 1
        assert len(pool) == 1

    def test_spellings_of_one_path_are_one_entry(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        first = pool.get(root / "board-a", page_size=PAGE_SIZE)
        second = pool.get(root / "." / "board-a", page_size=PAGE_SIZE)
        third = pool.get(root / "other" / ".." / "board-a", page_size=PAGE_SIZE)

        assert first is second is third
        assert len(connector.calls) == 1

    def test_different_databases_get_different_handles(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        first = pool.get(root / "board-a", page_size=PAGE_SIZE)
        second = pool.get(root / "board-b", page_size=PAGE_SIZE)

        assert first is not second
        assert len(connector.calls) == 2
        assert len(pool) == 2

    def test_concurrent_callers_share_a_single_open(self, root: Path) -> None:
        connector = _Connector()
        # Hold the open long enough that every other thread arrives while the
        # first is still inside connect.
        connector.open_delay_seconds = 0.2
        pool = _pool(root, connector)
        results: list[Any] = []
        errors: list[BaseException] = []

        def acquire() -> None:
            try:
                results.append(pool.get(root / "board-a", page_size=PAGE_SIZE))
            except BaseException as failure:  # noqa: BLE001 - surfaced below
                errors.append(failure)

        threads = [threading.Thread(target=acquire) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert len(results) == 8
        assert all(handle is results[0] for handle in results)
        # The whole point: eight callers, one open.
        assert len(connector.calls) == 1


class TestGeometryIsPartOfIdentity:
    def test_a_different_page_size_on_the_same_path_is_refused(
        self, root: Path
    ) -> None:
        connector = _Connector()
        pool = _pool(root, connector)
        pool.get(root / "board-a", page_size=PAGE_SIZE)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / "board-a", page_size=16384)

        assert refused.value.reason == "pool_page_size_mismatch"
        assert refused.value.details["pooled_page_size"] == PAGE_SIZE
        assert refused.value.details["requested_page_size"] == 16384
        # The refusal changes nothing: the original handle is still the one.
        assert len(connector.calls) == 1
        assert len(pool) == 1

    @pytest.mark.parametrize("page_size", [0, 1024, 2048, 4095, 12288, 65536, -8192])
    def test_an_invalid_page_size_never_reaches_connect(
        self, root: Path, page_size: int
    ) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / "board-a", page_size=page_size)

        assert refused.value.reason == "pool_page_size_invalid"
        assert connector.calls == []
        assert len(pool) == 0


class TestNothingPartiallyOpenIsPublished:
    def test_a_failed_connect_caches_nothing(self, root: Path) -> None:
        connector = _Connector()
        connector.failure = OSError("injected connect failure")
        pool = _pool(root, connector)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / "board-a", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_open_failed"
        assert len(pool) == 0

        # A later caller gets a real attempt rather than a cached failure.
        connector.failure = None
        handle = pool.get(root / "board-a", page_size=PAGE_SIZE)
        assert handle is not None
        assert len(connector.calls) == 2
        assert len(pool) == 1

    def test_a_database_that_fails_admission_is_closed_and_not_cached(
        self, root: Path
    ) -> None:
        # The handle reports a geometry other than the one requested, so
        # admission must refuse it.
        connector = _Connector(page_size=16384)
        pool = _pool(root, connector)

        with pytest.raises(GraphCapabilityUnavailable) as refused:
            pool.get(root / "board-a", page_size=PAGE_SIZE)

        assert (
            refused.value.details["reason"] == "grafx_page_size_configuration_mismatch"
        )
        assert len(pool) == 0
        assert connector.databases[0].close_calls == 1
        assert connector.databases[0].closed is True

    def test_a_path_mismatch_fails_admission_and_is_not_cached(
        self, root: Path
    ) -> None:
        class _WrongPathConnector(_Connector):
            def __call__(self, path: Path, *, page_size: int) -> _FakeDatabase:
                database = super().__call__(path, page_size=page_size)
                database.path = str(Path(path).parent / "somewhere-else")
                return database

        connector = _WrongPathConnector()
        pool = _pool(root, connector)

        with pytest.raises(GraphCapabilityUnavailable) as refused:
            pool.get(root / "board-a", page_size=PAGE_SIZE)

        assert refused.value.details["reason"] == "grafx_database_path_mismatch"
        assert len(pool) == 0
        assert connector.databases[0].close_calls == 1


class TestContainment:
    def test_a_path_outside_the_root_is_refused_before_opening(
        self, root: Path, tmp_path: Path
    ) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(tmp_path / "outside", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_path_escapes_root"
        assert connector.calls == []

    def test_a_traversal_escape_is_refused(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / ".." / "escaped", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_path_escapes_root"
        assert connector.calls == []

    def test_the_root_itself_is_not_a_database(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root, page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_path_is_root"
        assert connector.calls == []


class TestAliasesAreRefusedBeforeOpening:
    """Junctions on Python 3.11, where pathlib alone cannot see them."""

    @pytest.fixture
    def without_is_junction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for owner in Path.__mro__:
            if "is_junction" in owner.__dict__:
                monkeypatch.delattr(owner, "is_junction", raising=False)
        assert not hasattr(Path, "is_junction")

    def test_a_junctioned_leaf_is_refused_and_the_target_untouched(
        self, root: Path, tmp_path: Path, without_is_junction: None
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_bytes(b"external sentinel\n")

        connector = _Connector()
        pool = _pool(root, connector)
        if not _make_junction(root / "board-a", outside):
            pytest.skip("host cannot create a Windows junction for this safety probe")
        assert (root / "board-a").is_symlink() is False

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / "board-a", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_path_is_alias"
        assert connector.calls == []
        assert sentinel.read_bytes() == b"external sentinel\n"
        assert sorted(path.name for path in outside.iterdir()) == ["sentinel.txt"]

    def test_a_junctioned_parent_is_refused(
        self, root: Path, tmp_path: Path, without_is_junction: None
    ) -> None:
        outside = tmp_path / "outside"
        (outside / "board-a").mkdir(parents=True)

        connector = _Connector()
        pool = _pool(root, connector)
        if not _make_junction(root / "tenant", outside):
            pytest.skip("host cannot create a Windows junction for this safety probe")

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / "tenant" / "board-a", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_path_is_alias"
        assert connector.calls == []

    def test_a_junctioned_root_is_refused(
        self, tmp_path: Path, without_is_junction: None
    ) -> None:
        real_root = tmp_path / "real-kg"
        (real_root / "board-a").mkdir(parents=True)
        linked_root = tmp_path / "linked-kg"
        if not _make_junction(linked_root, real_root):
            pytest.skip("host cannot create a Windows junction for this safety probe")

        connector = _Connector()
        pool = _pool(linked_root, connector)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(linked_root / "board-a", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_root_is_alias"
        assert connector.calls == []


class TestReleaseIsIdempotentAndKeepsWhatItCannotClose:
    def test_close_then_reopen_opens_again(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)
        first = pool.get(root / "board-a", page_size=PAGE_SIZE)

        assert pool.close(root / "board-a") is True
        assert first.closed is True
        assert len(pool) == 0
        # Idempotent: closing what is gone is False, not an error.
        assert pool.close(root / "board-a") is False

        second = pool.get(root / "board-a", page_size=PAGE_SIZE)
        assert second is not first
        assert len(connector.calls) == 2

    def test_a_failed_close_keeps_the_handle_reachable(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)
        database = pool.get(root / "board-a", page_size=PAGE_SIZE)
        database.close_failures = 1

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.close(root / "board-a")

        assert refused.value.reason == "pool_close_failed"
        # Still pooled, so a retry has something to reach.
        assert len(pool) == 1
        assert pool.get(root / "board-a", page_size=PAGE_SIZE) is database

        assert pool.close(root / "board-a") is True
        assert database.close_calls == 2
        assert len(pool) == 0

    def test_close_all_attempts_every_handle_and_keeps_the_unclosed(
        self, root: Path
    ) -> None:
        connector = _Connector()
        pool = _pool(root, connector)
        first = pool.get(root / "board-a", page_size=PAGE_SIZE)
        stubborn = pool.get(root / "board-b", page_size=PAGE_SIZE)
        third = pool.get(root / "board-c", page_size=PAGE_SIZE)
        stubborn.close_failures = 1

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.close_all()

        assert refused.value.reason == "pool_close_all_partial"
        assert refused.value.details["closed"] == 2
        assert refused.value.details["remaining"] == 1
        # Every handle was attempted, not just the ones before the failure.
        assert first.closed is True
        assert third.closed is True
        assert stubborn.closed is False
        assert len(pool) == 1

        assert pool.close_all() == 1
        assert stubborn.closed is True
        assert len(pool) == 0

    def test_close_all_on_an_empty_pool_is_zero(self, root: Path) -> None:
        pool = _pool(root, _Connector())
        assert pool.close_all() == 0
        assert pool.close_all() == 0

    def test_concurrent_close_all_closes_each_handle_once(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)
        handles = [
            pool.get(root / f"board-{index}", page_size=PAGE_SIZE) for index in range(6)
        ]
        errors: list[BaseException] = []

        def release() -> None:
            try:
                pool.close_all()
            except BaseException as failure:  # noqa: BLE001 - surfaced below
                errors.append(failure)

        threads = [threading.Thread(target=release) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert len(pool) == 0
        assert all(handle.close_calls == 1 for handle in handles)


class TestIntrospection:
    def test_pooled_paths_names_what_is_held(self, root: Path) -> None:
        pool = _pool(root, _Connector())
        pool.get(root / "board-a", page_size=PAGE_SIZE)
        pool.get(root / "board-b", page_size=PAGE_SIZE)

        pooled = pool.pooled_paths()

        assert len(pooled) == 2
        assert all("board-" in entry for entry in pooled)

    def test_borrow_shares_the_pooled_handle_without_closing_it(
        self, root: Path
    ) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        with pool.borrow(root / "board-a", page_size=PAGE_SIZE) as borrowed:
            assert borrowed is pool.get(root / "board-a", page_size=PAGE_SIZE)

        # Leaving the block returns nothing to close: the pool still owns it.
        assert borrowed.closed is False
        assert len(pool) == 1
        assert len(connector.calls) == 1


# --- the bound, and what may be closed to honour it ----------------------------------------------


class TestTheBoundIsEnforcedBeforeOpening:
    @pytest.mark.parametrize("max_entries", [0, -1, 1.5, "2", None.__class__])
    def test_an_invalid_bound_refuses_to_build_the_pool(
        self, root: Path, max_entries: object
    ) -> None:
        with pytest.raises(GrafxDatabasePoolError) as refused:
            CommunityGrafxDatabasePool(
                root, connect=_Connector(), max_entries=max_entries
            )

        assert refused.value.reason == "pool_max_entries_invalid"

    def test_an_unbounded_pool_keeps_every_handle(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        for index in range(5):
            pool.get(root / f"board-{index}", page_size=PAGE_SIZE)

        assert len(pool) == 5

    def test_the_bound_is_never_exceeded(self, root: Path) -> None:
        connector = _Connector()
        pool = CommunityGrafxDatabasePool(root, connect=connector, max_entries=2)

        for index in range(5):
            pool.get(root / f"board-{index}", page_size=PAGE_SIZE)
            assert len(pool) <= 2

        assert len(pool) == 2

    def test_the_coldest_unpinned_entry_is_the_one_evicted(self, root: Path) -> None:
        connector = _Connector()
        pool = CommunityGrafxDatabasePool(root, connect=connector, max_entries=2)
        first = pool.get(root / "board-a", page_size=PAGE_SIZE)
        pool.get(root / "board-b", page_size=PAGE_SIZE)
        # Touching board-a makes board-b the coldest.
        assert pool.get(root / "board-a", page_size=PAGE_SIZE) is first

        pool.get(root / "board-c", page_size=PAGE_SIZE)

        pooled = pool.pooled_paths()
        assert any("board-a" in entry for entry in pooled)
        assert any("board-c" in entry for entry in pooled)
        assert not any("board-b" in entry for entry in pooled)
        assert first.closed is False

    def test_a_leased_entry_is_never_the_victim(self, root: Path) -> None:
        connector = _Connector()
        pool = CommunityGrafxDatabasePool(root, connect=connector, max_entries=2)
        lease = pool.acquire(root / "board-a", page_size=PAGE_SIZE)
        pool.get(root / "board-b", page_size=PAGE_SIZE)

        pool.get(root / "board-c", page_size=PAGE_SIZE)

        # board-a is the coldest but it is leased, so board-b goes instead.
        assert any("board-a" in entry for entry in pool.pooled_paths())
        assert not any("board-b" in entry for entry in pool.pooled_paths())
        assert lease.database.closed is False
        lease.release()

    def test_a_full_pool_of_leases_refuses_rather_than_evicting(
        self, root: Path
    ) -> None:
        connector = _Connector()
        pool = CommunityGrafxDatabasePool(root, connect=connector, max_entries=2)
        leases = [
            pool.acquire(root / "board-a", page_size=PAGE_SIZE),
            pool.acquire(root / "board-b", page_size=PAGE_SIZE),
        ]
        opens_before = len(connector.calls)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / "board-c", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_exhausted_all_pinned"
        # Nothing was opened and nothing was closed to try to make room.
        assert len(connector.calls) == opens_before
        assert all(lease.database.closed is False for lease in leases)
        assert len(pool) == 2

        leases[0].release()
        pool.get(root / "board-c", page_size=PAGE_SIZE)
        assert len(pool) == 2

    def test_an_eviction_that_cannot_close_opens_nothing(self, root: Path) -> None:
        connector = _Connector()
        pool = CommunityGrafxDatabasePool(root, connect=connector, max_entries=1)
        stubborn = pool.get(root / "board-a", page_size=PAGE_SIZE)
        stubborn.close_failures = 1
        opens_before = len(connector.calls)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.get(root / "board-b", page_size=PAGE_SIZE)

        assert refused.value.reason == "pool_eviction_close_failed"
        # No new handle, and the victim is still tracked rather than orphaned.
        assert len(connector.calls) == opens_before
        assert len(pool) == 1
        assert pool.get(root / "board-a", page_size=PAGE_SIZE) is stubborn


class TestLeases:
    def test_a_lease_exposes_the_pooled_handle(self, root: Path) -> None:
        connector = _Connector()
        pool = _pool(root, connector)

        lease = pool.acquire(root / "board-a", page_size=PAGE_SIZE)

        assert isinstance(lease, GrafxDatabaseLease)
        assert lease.database is pool.get(root / "board-a", page_size=PAGE_SIZE)
        assert pool.pin_count(root / "board-a") == 1
        assert len(connector.calls) == 1
        lease.release()

    def test_release_is_idempotent_and_counts_once(self, root: Path) -> None:
        pool = _pool(root, _Connector())
        lease = pool.acquire(root / "board-a", page_size=PAGE_SIZE)

        assert lease.release() is True
        assert lease.released is True
        # Every later release is a no-op, not a second decrement that would
        # free an entry somebody else still holds.
        assert lease.release() is False
        assert lease.release() is False
        assert pool.pin_count(root / "board-a") == 0

    def test_two_leases_on_one_database_both_have_to_be_released(
        self, root: Path
    ) -> None:
        pool = _pool(root, _Connector())
        first = pool.acquire(root / "board-a", page_size=PAGE_SIZE)
        second = pool.acquire(root / "board-a", page_size=PAGE_SIZE)

        assert pool.pin_count(root / "board-a") == 2
        first.release()
        assert pool.pin_count(root / "board-a") == 1
        with pytest.raises(GrafxDatabasePoolError):
            pool.close(root / "board-a")

        second.release()
        assert pool.pin_count(root / "board-a") == 0
        assert pool.close(root / "board-a") is True

    def test_a_lease_is_a_context_manager(self, root: Path) -> None:
        pool = _pool(root, _Connector())

        with pool.acquire(root / "board-a", page_size=PAGE_SIZE) as lease:
            assert pool.pin_count(root / "board-a") == 1
            assert lease.database is not None

        assert pool.pin_count(root / "board-a") == 0

    def test_borrow_pins_and_releases_even_when_the_body_raises(
        self, root: Path
    ) -> None:
        pool = _pool(root, _Connector())

        with (
            pytest.raises(RuntimeError),
            pool.borrow(root / "board-a", page_size=PAGE_SIZE) as database,
        ):
            assert pool.pin_count(root / "board-a") == 1
            assert database is not None
            raise RuntimeError("injected body failure")

        assert pool.pin_count(root / "board-a") == 0
        assert len(pool) == 1


class TestLeasedEntriesAreNotClosed:
    def test_close_refuses_a_leased_entry_without_closing_it(self, root: Path) -> None:
        pool = _pool(root, _Connector())
        lease = pool.acquire(root / "board-a", page_size=PAGE_SIZE)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.close(root / "board-a")

        assert refused.value.reason == "pool_close_refused_pinned"
        assert refused.value.details["pins"] == 1
        assert lease.database.close_calls == 0
        assert len(pool) == 1

        lease.release()
        assert pool.close(root / "board-a") is True

    def test_close_all_closes_the_free_ones_and_keeps_the_leased(
        self, root: Path
    ) -> None:
        pool = _pool(root, _Connector())
        free = pool.get(root / "board-a", page_size=PAGE_SIZE)
        lease = pool.acquire(root / "board-b", page_size=PAGE_SIZE)
        other = pool.get(root / "board-c", page_size=PAGE_SIZE)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.close_all()

        assert refused.value.reason == "pool_close_all_partial"
        assert refused.value.details["closed"] == 2
        assert refused.value.details["remaining"] == 1
        assert len(refused.value.details["pinned"]) == 1
        assert free.closed is True
        assert other.closed is True
        # The leased handle is untouched: closing it would be a use-after-close.
        assert lease.database.close_calls == 0

        lease.release()
        assert pool.close_all() == 1
        assert lease.database.closed is True

    def test_close_all_reports_leases_and_failures_together(self, root: Path) -> None:
        pool = _pool(root, _Connector())
        pool.get(root / "board-a", page_size=PAGE_SIZE)
        stubborn = pool.get(root / "board-b", page_size=PAGE_SIZE)
        stubborn.close_failures = 1
        lease = pool.acquire(root / "board-c", page_size=PAGE_SIZE)

        with pytest.raises(GrafxDatabasePoolError) as refused:
            pool.close_all()

        details = refused.value.details
        assert details["closed"] == 1
        assert details["remaining"] == 2
        assert len(details["failures"]) == 1
        assert len(details["pinned"]) == 1
        lease.release()


class TestConcurrencyNeverClosesAHandleInUse:
    def test_close_all_races_against_leases_without_closing_a_leased_handle(
        self, root: Path
    ) -> None:
        pool = _pool(root, _Connector())
        paths = [root / f"board-{index}" for index in range(6)]
        for path in paths:
            pool.get(path, page_size=PAGE_SIZE)
        observed: list[bool] = []
        errors: list[BaseException] = []
        stop = threading.Event()

        def hold() -> None:
            try:
                while not stop.is_set():
                    with pool.borrow(paths[0], page_size=PAGE_SIZE) as database:
                        # While the lease is held the handle must be usable, so
                        # a close that slipped through would be visible here.
                        observed.append(database.closed)
            except BaseException as failure:  # noqa: BLE001 - surfaced below
                errors.append(failure)

        def sweep() -> None:
            for _ in range(40):
                try:
                    pool.close_all()
                except GrafxDatabasePoolError:
                    pass
                except BaseException as failure:  # noqa: BLE001 - surfaced below
                    errors.append(failure)

        holder = threading.Thread(target=hold)
        sweeper = threading.Thread(target=sweep)
        holder.start()
        sweeper.start()
        sweeper.join(timeout=30)
        stop.set()
        holder.join(timeout=30)

        assert errors == []
        assert observed
        # Not one observation saw a closed database through a live lease.
        assert not any(observed)


class TestReleaseIsExactlyOnceUnderContention:
    """Two threads releasing one lease must decrement exactly one pin.

    The check and the set are separate bytecodes, so a flag tested outside the
    pool's lock lets both threads observe "not released" and both decrement --
    freeing an entry a third caller still holds. The claim therefore happens
    inside the lock, and this test is what proves it rather than the GIL.
    """

    def test_release_claims_nothing_until_it_holds_the_pool_lock(
        self, root: Path
    ) -> None:
        """The deterministic form of the race, rather than hoping to hit it.

        Racing two threads does not expose this: the window between testing the
        flag and setting it is a couple of bytecodes and the GIL almost never
        switches inside it -- I measured 200 contended releases with the old
        implementation and saw zero bad outcomes. What IS decidable is WHERE the
        claim happens. Holding the pool's lock from here freezes any correct
        release before it can claim anything; an implementation that sets its
        flag first would show the claim already taken while still blocked.
        """

        pool = _pool(root, _Connector())
        lease = pool.acquire(root / "board-a", page_size=PAGE_SIZE)
        started = threading.Event()
        finished = threading.Event()

        def release_from_another_thread() -> None:
            started.set()
            lease.release()
            finished.set()

        thread = threading.Thread(target=release_from_another_thread)
        with pool._lock:
            thread.start()
            assert started.wait(timeout=5)
            time.sleep(0.25)
            # Blocked on the lock, so nothing may have been claimed yet.
            assert lease.released is False
            assert finished.is_set() is False
        thread.join(timeout=5)

        assert lease.released is True
        assert pool.pin_count(root / "board-a") == 0

    def test_contended_releases_free_exactly_one_pin(self, root: Path) -> None:
        pool = _pool(root, _Connector())

        for attempt in range(25):
            path = root / f"board-{attempt}"
            keeper = pool.acquire(path, page_size=PAGE_SIZE)
            doomed = pool.acquire(path, page_size=PAGE_SIZE)
            start = threading.Barrier(2)
            winners: list[bool] = []
            lock = threading.Lock()

            def race(
                lease: GrafxDatabaseLease = doomed,
                guard: threading.Lock = lock,
                results: list[bool] = winners,
                gate: threading.Barrier = start,
            ) -> None:
                gate.wait(timeout=10)
                won = lease.release()
                with guard:
                    results.append(won)

            threads = [threading.Thread(target=race) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            assert winners.count(True) == 1
            assert pool.pin_count(path) == 1
            keeper.release()

    def test_a_lost_release_cannot_free_another_holders_pin(self, root: Path) -> None:
        pool = _pool(root, _Connector())
        path = root / "board-a"
        first = pool.acquire(path, page_size=PAGE_SIZE)
        second = pool.acquire(path, page_size=PAGE_SIZE)

        first.release()
        # Repeated releases of a spent lease must never touch the other pin.
        for _ in range(10):
            assert first.release() is False
        assert pool.pin_count(path) == 1

        with pytest.raises(GrafxDatabasePoolError):
            pool.close(path)
        second.release()
        assert pool.close(path) is True
