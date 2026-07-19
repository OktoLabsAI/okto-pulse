from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    RecoveryStoreContractError,
)
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryProgressCounts,
    RecoveryRunBinding,
    RecoveryStartCommand,
)


def _obsolete_direct_start(run_id: str) -> RecoveryStartCommand:
    return RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id=run_id,
            actor_id="agent-test",
            confirmation_fingerprint=f"sha256:{run_id}",
            manifest_ref=f"manifest://{run_id}",
            preflight_hash=f"preflight-{run_id}",
            reason="obsolete direct-start fixture",
        ),
        started_at=datetime.now(timezone.utc),
        counts=RecoveryProgressCounts(sources_total=1),
    )


@pytest.mark.parametrize(
    "run_id",
    [
        "run-future-failure",
        "run-cancel-conflict",
        "run-missing-input",
    ],
)
def test_pre_i2_direct_worker_fixture_is_rejected_by_two_stage_contract(
    tmp_path: Path,
    recovery_store_factory,
    run_id: str,
) -> None:
    """R5 never synthesizes preparation metadata for a direct RECOVERY run."""

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / f'{run_id}.sqlite3').as_posix()}"
    )
    with pytest.raises(RecoveryStoreContractError) as rejected:
        store.create_run(_obsolete_direct_start(run_id))
    assert rejected.value.code == "global_discovery_recovery_two_stage_required"
    assert store.get_status(run_id=run_id) is None
