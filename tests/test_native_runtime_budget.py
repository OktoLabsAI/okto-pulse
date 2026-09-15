from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from okto_pulse.community.api.kg_health import NativeRuntimeBudget
from okto_pulse.community.config import CommunitySettings


_BUDGET_ENV_VARS = (
    "KG_DB_CACHE_CAP",
    "OKTO_PULSE_COMMUNITY_KG_BOARD_BUFFER_POOL_CAP_MB",
    "OKTO_PULSE_COMMUNITY_KG_MAX_DB_SIZE_CAP_GB",
)


def _settings(tmp_path: Path, **overrides: int) -> CommunitySettings:
    values = {
        "data_dir": str(tmp_path),
        "kg_kuzu_buffer_pool_mb": 256,
        "kg_global_kuzu_buffer_pool_mb": 128,
        "kg_kuzu_max_db_size_gb": 2,
        "kg_connection_pool_size": 2,
    }
    values.update(overrides)
    return CommunitySettings(_env_file=None, **values)


def test_rest_budget_model_is_strict_and_accepts_fail_closed_empty_maps() -> None:
    unavailable = NativeRuntimeBudget.model_validate(
        {
            "source": "runtime_capability",
            "status": "unavailable",
            "requested": {},
            "normalized": {},
            "effective": {},
            "sources": {},
            "process_envelope": {},
            "is_direct_memory_telemetry": False,
            "description": "Derived non-live budget.",
            "tooltip": "Not RSS or direct telemetry.",
            "unavailable_reason": "budget_snapshot_unavailable",
        }
    )

    assert unavailable.status == "unavailable"
    assert unavailable.effective.board_buffer_pool_mb is None
    with pytest.raises(ValidationError):
        NativeRuntimeBudget.model_validate(
            {
                **unavailable.model_dump(),
                "resident_board_ids": ["private-board"],
            }
        )
    with pytest.raises(ValidationError):
        NativeRuntimeBudget.model_validate(
            {
                **unavailable.model_dump(),
                "effective": {"backend_path": r"C:\private\graph.lbug"},
            }
        )
