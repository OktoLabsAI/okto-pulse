from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    RecoveryDispatchStage,
    SQLAlchemyRecoveryRunStore,
)
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryDispatchClaimConflict,
    RecoveryPreparationCommand,
    RecoveryProgressCounts,
    RecoveryRunBinding,
)


NOW = datetime(2026, 7, 16, 15, 15, tzinfo=timezone.utc)


def _admit_and_claim(
    store: SQLAlchemyRecoveryRunStore,
    *,
    run_id: str,
):
    admitted, created = store.admit_preparation(
        RecoveryPreparationCommand(
            binding=RecoveryRunBinding(
                run_id=run_id,
                actor_id="agent-test",
            ),
            admitted_at=NOW,
            counts=RecoveryProgressCounts(sources_total=2),
            attempt_budget_ms=60_000,
        )
    )
    assert created is True
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="worker-before-restart",
        claimed_at=NOW,
        claim_expires_at=NOW + timedelta(seconds=15),
    )
    assert claim is not None
    store.mark_preparing(
        run_id=run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=claim.claim_token,
        at=NOW,
    )
    return admitted, claim


def test_restart_requires_expired_claim_then_fences_old_token(
    tmp_path: Path,
    recovery_store_factory,
) -> None:
    """I2 restarts reclaim the same durable preparation attempt, not a new epoch."""

    database_url = f"sqlite:///{(tmp_path / 'takeover.sqlite3').as_posix()}"
    first_store = recovery_store_factory(database_url)
    admitted, first_claim = _admit_and_claim(
        first_store,
        run_id="run-takeover",
    )
    first_store.engine.dispose()

    restarted = recovery_store_factory(database_url)
    assert (
        restarted.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id="worker-too-early",
            claimed_at=NOW + timedelta(milliseconds=14_999),
            claim_expires_at=NOW + timedelta(milliseconds=29_999),
        )
        is None
    )

    reclaimed = restarted.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="worker-after-restart",
        claimed_at=NOW + timedelta(seconds=15),
        claim_expires_at=NOW + timedelta(seconds=30),
    )
    assert reclaimed is not None
    assert reclaimed.run_id == admitted.run_id
    assert reclaimed.attempt_id == admitted.attempt_id
    assert reclaimed.epoch == admitted.epoch
    assert reclaimed.claim_token != first_claim.claim_token
    assert reclaimed.attempt_count == 2

    with pytest.raises(RecoveryDispatchClaimConflict):
        restarted.record_preparation_failure(
            dispatch_id=first_claim.dispatch_id,
            claim_token=first_claim.claim_token,
            failed_at=NOW + timedelta(seconds=16),
            active_elapsed_ms=16_000,
            counts=RecoveryProgressCounts(sources_total=2, errors=1),
            reason_code="stale_worker_failure",
            retryable=False,
            retry_available_at=NOW + timedelta(seconds=17),
            max_attempts=3,
        )


def test_two_process_contenders_reclaim_exactly_one_expired_dispatch(
    tmp_path: Path,
    recovery_store_factory,
) -> None:
    database_url = (
        f"sqlite:///{(tmp_path / 'takeover-race.sqlite3').as_posix()}"
    )
    seed = recovery_store_factory(database_url)
    admitted, first_claim = _admit_and_claim(
        seed,
        run_id="run-takeover-race",
    )
    seed.engine.dispose()

    stores = [
        recovery_store_factory(database_url),
        recovery_store_factory(database_url),
    ]
    ready = Barrier(3)

    def contend(store: SQLAlchemyRecoveryRunStore):
        ready.wait(timeout=2)
        return store.claim_next_dispatch(
            stage=RecoveryDispatchStage.PREPARATION,
            worker_id=f"contender-{id(store)}",
            claimed_at=first_claim.claim_expires_at,
            claim_expires_at=first_claim.claim_expires_at
            + timedelta(seconds=15),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contend, store) for store in stores]
        ready.wait(timeout=2)
        results = [future.result(timeout=10) for future in futures]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].run_id == admitted.run_id
    assert winners[0].attempt_id == admitted.attempt_id
    assert winners[0].epoch == admitted.epoch
    assert winners[0].attempt_count == 2
