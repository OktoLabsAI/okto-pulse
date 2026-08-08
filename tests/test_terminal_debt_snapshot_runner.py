from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.terminal_debt_snapshot import (
    IsolatedCopyTerminalDebtRunner,
    SnapshotExecutionDenied,
)
from okto_pulse.community.adapters.terminal_debt_source import (
    TerminalDebtSourceIdentityError,
    attest_sqlite_terminal_debt_snapshot,
    sqlalchemy_source_fingerprint,
    sqlite_storage_fingerprint,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.domain.terminal_debt import (
    TerminalDebtActionOwner,
    TerminalDebtCopyAction,
    TerminalDebtDomain,
    TerminalDebtExecutionOutcome,
    TerminalDebtExecutionResult,
    TerminalDebtIdentity,
    TerminalDebtItem,
    TerminalDebtManifest,
    TerminalDebtRefusalCode,
)


def _item(
    value: str,
    marker: str,
    *,
    replay_safe: bool = True,
) -> TerminalDebtItem:
    return TerminalDebtItem(
        identity=TerminalDebtIdentity(
            TerminalDebtDomain.CONSOLIDATION_DLQ,
            value,
        ),
        recovery_class="terminal_consolidation_delivery",
        replay_safe=replay_safe,
        action_owner=(
            TerminalDebtActionOwner.AUTOMATION
            if replay_safe
            else TerminalDebtActionOwner.HUMAN
        ),
        source_version=1,
        content_hash=canonical_sha256({"value": value, "marker": marker}),
        copy_action=(
            TerminalDebtCopyAction.REQUEUE_CONSOLIDATION_COPY if replay_safe else None
        ),
        attributes=(("marker", marker),),
    )


def _rows(path: Path) -> tuple[tuple[str, str], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute("SELECT id, marker FROM debt ORDER BY id").fetchall()
        )


def _manifest(
    path: Path,
    *,
    fingerprint: str,
    replay_safe: bool = True,
) -> TerminalDebtManifest:
    return TerminalDebtManifest(
        domain=TerminalDebtDomain.CONSOLIDATION_DLQ,
        scope_id="board-runner",
        source_fingerprint=fingerprint,
        items=tuple(
            _item(value, marker, replay_safe=replay_safe)
            for value, marker in _rows(path)
        ),
    )


def _snapshot(tmp_path: Path):
    origin = tmp_path / "origin.db"
    copy = tmp_path / "copy.db"
    with sqlite3.connect(origin) as connection:
        connection.execute(
            "CREATE TABLE debt (id TEXT PRIMARY KEY NOT NULL, marker TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO debt (id, marker) VALUES (?, ?)",
            (("selected", "original"), ("untouched", "original")),
        )
        connection.commit()
    shutil.copy2(origin, copy)
    isolation = attest_sqlite_terminal_debt_snapshot(
        origin_path=origin,
        copy_path=copy,
    )
    return origin, copy, isolation


class SqliteRecordingCopyExecutor:
    domain = TerminalDebtDomain.CONSOLIDATION_DLQ

    def __init__(
        self,
        *,
        target_path: Path,
        before: TerminalDebtManifest,
        declared_fingerprint: str | None = None,
        invalid_results: bool = False,
    ) -> None:
        self.target_path = target_path
        self.target_fingerprint = (
            declared_fingerprint
            if declared_fingerprint is not None
            else sqlite_storage_fingerprint(target_path)
        )
        self._before = before.item_map()
        self._invalid_results = invalid_results
        self.calls = 0

    async def execute(self, plan):
        self.calls += 1
        if self._invalid_results:
            return (object(),)
        with sqlite3.connect(self.target_path) as connection:
            connection.executemany(
                "DELETE FROM debt WHERE id = ?",
                ((identity.value,) for identity in plan.selected_identities),
            )
            connection.commit()
        return tuple(
            TerminalDebtExecutionResult(
                identity=identity,
                outcome=TerminalDebtExecutionOutcome.RESOLVED,
                before_item_digest=self._before[identity].item_digest,
                after_item_digest=None,
                evidence_hash=canonical_sha256(
                    {
                        "plan_digest": plan.plan_digest,
                        "identity": identity.as_dict(),
                    }
                ),
            )
            for identity in plan.selected_identities
        )


def test_runner_rejects_ambiguous_or_untyped_executor_configuration() -> None:
    with pytest.raises(SnapshotExecutionDenied, match="copy_executors_invalid"):
        IsolatedCopyTerminalDebtRunner("not-executors")  # type: ignore[arg-type]

    class MissingDomain:
        async def execute(self, _plan):
            return ()

    with pytest.raises(SnapshotExecutionDenied, match="copy_executor_domain_invalid"):
        IsolatedCopyTerminalDebtRunner((MissingDomain(),))  # type: ignore[arg-type]

    class DuplicateDomain:
        domain = TerminalDebtDomain.CONSOLIDATION_DLQ

    with pytest.raises(SnapshotExecutionDenied, match="copy_executor_domain_duplicate"):
        IsolatedCopyTerminalDebtRunner((DuplicateDomain(), DuplicateDomain()))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runner_uses_distinct_sqlite_copy_and_proves_origin_bytes_unchanged(
    tmp_path: Path,
) -> None:
    origin, copy, isolation = _snapshot(tmp_path)
    origin_bytes_before = origin.read_bytes()
    origin_rows_before = _rows(origin)
    origin_before = _manifest(origin, fingerprint=isolation.origin_fingerprint)
    copy_before = _manifest(copy, fingerprint=isolation.copy_fingerprint)
    executor = SqliteRecordingCopyExecutor(
        target_path=copy,
        before=copy_before,
    )
    runner = IsolatedCopyTerminalDebtRunner((executor,))

    report = await runner.run(
        isolation=isolation,
        origin_before=origin_before,
        copy_before=copy_before,
        selection=(
            origin_before.item_map()[
                TerminalDebtIdentity(TerminalDebtDomain.CONSOLIDATION_DLQ, "selected")
            ].identity,
        ),
        read_origin_after=lambda: _async_manifest(origin, isolation.origin_fingerprint),
        read_copy_after=lambda: _async_manifest(copy, isolation.copy_fingerprint),
    )

    assert executor.calls == 1
    assert origin.read_bytes() == origin_bytes_before
    assert _rows(origin) == origin_rows_before
    assert _rows(copy) == (("untouched", "original"),)
    assert report.origin_bytes_unchanged
    assert report.verified
    assert report.proof is not None and report.proof.verified


async def _async_manifest(path: Path, fingerprint: str) -> TerminalDebtManifest:
    return _manifest(path, fingerprint=fingerprint)


def test_attestation_denies_origin_copy_alias(tmp_path: Path) -> None:
    # A same-file alias is rejected by filesystem identity, without relying on
    # two caller-provided names or hashes.
    origin, _copy, _isolation = _snapshot(tmp_path)
    with pytest.raises(TerminalDebtSourceIdentityError, match="origin_copy_alias"):
        attest_sqlite_terminal_debt_snapshot(
            origin_path=origin,
            copy_path=origin,
        )


@pytest.mark.asyncio
async def test_same_sqlite_store_has_one_fingerprint_regardless_of_engine_label(
    tmp_path: Path,
) -> None:
    origin, _copy, _isolation = _snapshot(tmp_path)
    first_engine = create_async_engine(f"sqlite+aiosqlite:///{origin}")
    second_engine = create_async_engine(f"sqlite+aiosqlite:///{origin}")
    try:
        first = async_sessionmaker(first_engine)
        second = async_sessionmaker(second_engine)
        assert sqlalchemy_source_fingerprint(first) == sqlalchemy_source_fingerprint(
            second
        )
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("origin", "copy"))
async def test_file_change_after_attestation_denies_before_executor(
    tmp_path: Path,
    changed: str,
) -> None:
    origin, copy, isolation = _snapshot(tmp_path)
    origin_before = _manifest(origin, fingerprint=isolation.origin_fingerprint)
    copy_before = _manifest(copy, fingerprint=isolation.copy_fingerprint)
    executor = SqliteRecordingCopyExecutor(target_path=copy, before=copy_before)
    target = origin if changed == "origin" else copy
    with sqlite3.connect(target) as connection:
        connection.execute("INSERT INTO debt (id, marker) VALUES ('late', 'changed')")
        connection.commit()

    runner = IsolatedCopyTerminalDebtRunner((executor,))
    with pytest.raises(
        SnapshotExecutionDenied,
        match=f"{changed}_changed_before_execution",
    ):
        await runner.run(
            isolation=isolation,
            origin_before=origin_before,
            copy_before=copy_before,
            selection=(origin_before.items[0].identity,),
            read_origin_after=lambda: _async_manifest(
                origin, isolation.origin_fingerprint
            ),
            read_copy_after=lambda: _async_manifest(copy, isolation.copy_fingerprint),
        )
    assert executor.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("origin", "false-declaration"))
async def test_unattested_executor_target_denies_before_command_and_preserves_origin(
    tmp_path: Path,
    target: str,
) -> None:
    origin, copy, isolation = _snapshot(tmp_path)
    origin_before_bytes = origin.read_bytes()
    origin_before = _manifest(origin, fingerprint=isolation.origin_fingerprint)
    copy_before = _manifest(copy, fingerprint=isolation.copy_fingerprint)
    executor = SqliteRecordingCopyExecutor(
        target_path=origin if target == "origin" else copy,
        before=copy_before,
        declared_fingerprint=(
            isolation.copy_fingerprint if target == "origin" else "f" * 64
        ),
    )

    with pytest.raises(SnapshotExecutionDenied, match="copy_target_mismatch"):
        await IsolatedCopyTerminalDebtRunner((executor,)).run(
            isolation=isolation,
            origin_before=origin_before,
            copy_before=copy_before,
            selection=(origin_before.items[0].identity,),
            read_origin_after=lambda: _async_manifest(
                origin, isolation.origin_fingerprint
            ),
            read_copy_after=lambda: _async_manifest(copy, isolation.copy_fingerprint),
        )
    assert executor.calls == 0
    assert origin.read_bytes() == origin_before_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "expected"),
    (
        ("empty", TerminalDebtRefusalCode.SELECTION_REQUIRED),
        ("replay_unsafe", TerminalDebtRefusalCode.REPLAY_UNSAFE),
    ),
)
async def test_plan_refusal_never_calls_executor_or_after_readers(
    tmp_path: Path,
    refusal: str,
    expected: TerminalDebtRefusalCode,
) -> None:
    origin, copy, isolation = _snapshot(tmp_path)
    replay_safe = refusal != "replay_unsafe"
    origin_before = _manifest(
        origin,
        fingerprint=isolation.origin_fingerprint,
        replay_safe=replay_safe,
    )
    copy_before = _manifest(
        copy,
        fingerprint=isolation.copy_fingerprint,
        replay_safe=replay_safe,
    )
    executor = SqliteRecordingCopyExecutor(target_path=copy, before=copy_before)
    after_reads = 0

    async def forbidden_read():
        nonlocal after_reads
        after_reads += 1
        raise AssertionError("after reader must not run for a refused plan")

    selection = () if refusal == "empty" else (origin_before.items[0].identity,)
    report = await IsolatedCopyTerminalDebtRunner((executor,)).run(
        isolation=isolation,
        origin_before=origin_before,
        copy_before=copy_before,
        selection=selection,
        read_origin_after=forbidden_read,
        read_copy_after=forbidden_read,
    )

    assert report.decision.refusal is not None
    assert report.decision.refusal.code is expected
    assert not report.executed
    assert executor.calls == 0
    assert after_reads == 0


@pytest.mark.asyncio
async def test_runner_rejects_non_evidence_executor_results(tmp_path: Path) -> None:
    origin, copy, isolation = _snapshot(tmp_path)
    origin_before = _manifest(origin, fingerprint=isolation.origin_fingerprint)
    copy_before = _manifest(copy, fingerprint=isolation.copy_fingerprint)
    executor = SqliteRecordingCopyExecutor(
        target_path=copy,
        before=copy_before,
        invalid_results=True,
    )

    with pytest.raises(SnapshotExecutionDenied, match="copy_executor_result_invalid"):
        await IsolatedCopyTerminalDebtRunner((executor,)).run(
            isolation=isolation,
            origin_before=origin_before,
            copy_before=copy_before,
            selection=(origin_before.items[0].identity,),
            read_origin_after=lambda: _async_manifest(
                origin, isolation.origin_fingerprint
            ),
            read_copy_after=lambda: _async_manifest(copy, isolation.copy_fingerprint),
        )
    assert executor.calls == 1
