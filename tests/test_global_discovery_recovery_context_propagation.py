from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from okto_pulse.community.adapters.composition import (
    configure_community_kg_registry,
)
from okto_pulse.community.adapters.global_discovery_recovery_preparation import (
    CommunityGlobalDiscoveryRecoveryPreparationOperation,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.kg.interfaces.registry import get_kg_registry
from okto_pulse.core.ports.global_discovery_recovery_control import (
    RecoveryProgressCounts,
)
from okto_pulse.core.ports.materialization_health import CensusStatus


class _ZeroSourceEvidencePort:
    async def current_generation(self, board_id: str) -> str:
        assert board_id == "zero-source-board"
        return "generation-zero"

    async def probe(self, request: object) -> object:
        assert getattr(request, "board_id") == "zero-source-board"
        state = SimpleNamespace(
            normalized_state=SimpleNamespace(value="confirmed_absent"),
            quarantined=False,
            reason_code="board_graph_confirmed_absent",
            generation="generation-zero",
        )
        return SimpleNamespace(
            board_store=state,
            discovery_store=SimpleNamespace(
                normalized_state=SimpleNamespace(value="confirmed_absent"),
                quarantined=False,
                reason_code="global_discovery_absent",
                generation="generation-zero",
            ),
            census=SimpleNamespace(
                status=CensusStatus.AVAILABLE,
                is_confirmed_zero=True,
                reason_code="relational_census_available",
                generation="generation-zero",
                source_count=0,
            ),
        )


def test_zero_source_seed_preserves_community_composition_in_worker_thread(
    tmp_path: Path,
) -> None:
    settings = CommunitySettings(
        data_dir=str(tmp_path),
        kg_base_dir=str(tmp_path / "kg"),
        kg_embedding_mode="stub",
        kg_embedding_dim=8,
    )
    configure_community_kg_registry(object(), settings=settings)
    registry = get_kg_registry()
    assert type(registry.embedding_provider).__name__ == (
        "CommunityStubEmbeddingProvider"
    )

    def reject_materialized_path(**_kwargs: object) -> object:
        raise AssertionError("zero-source board must not open a unit of work")

    operation = CommunityGlobalDiscoveryRecoveryPreparationOperation(
        recovery=object(),
        artifact_store=object(),
        db_path_provider=lambda: tmp_path / "unused.db",
        unit_of_work_factory=reject_materialized_path,
        materialization_evidence_port=_ZeroSourceEvidencePort(),
        max_parallel_boards=1,
    )
    board_row = {
        "board_id": "zero-source-board",
        "board_name": "Zero source board",
        "board_summary": "No sources yet",
    }
    inventory = SimpleNamespace(board_id="zero-source-board", source_count=0)
    captured = SimpleNamespace(
        board_rows=(board_row,),
        inventories=(inventory,),
        overlay=SimpleNamespace(),
    )
    checkpoints: list[RecoveryProgressCounts] = []

    async def prepare() -> tuple[tuple[object, ...], RecoveryProgressCounts]:
        _plans, seeds, progress = await operation._prepare_boards(  # noqa: SLF001
            captured=captured,
            fence_check=lambda **_kwargs: None,
            checkpoint=checkpoints.append,
            initial_progress=RecoveryProgressCounts(boards_total=1),
            deadline_at_monotonic=time.monotonic() + 5,
        )
        return seeds, progress

    seeds, progress = asyncio.run(prepare())

    assert len(seeds) == 1
    assert seeds[0].board_id == "zero-source-board"
    assert len(seeds[0].summary_embedding) == 8
    assert progress.boards_scanned == 1
    assert progress.sources_processed == 0
    assert progress.nodes_written == 1
    assert checkpoints == [progress]
