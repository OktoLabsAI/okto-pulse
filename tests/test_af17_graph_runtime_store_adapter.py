from __future__ import annotations

from okto_pulse.community.adapters.kuzu_graph_runtime_store import (
    CommunityKuzuGraphRuntimeStore,
)
from okto_pulse.community.adapters.kg import build_community_graph_providers
from okto_pulse.core.kg.interfaces.graph_runtime_store import GraphRuntimeStore


def test_af17_community_graph_runtime_store_footprint_uses_file_backing(
    tmp_path, monkeypatch
) -> None:
    graph_file = tmp_path / "board.lbug"
    graph_file.write_bytes(b"x" * 20)
    (tmp_path / "board.lbug.wal").write_bytes(b"y" * 5)

    from okto_pulse.community.adapters import kg_runtime

    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda board_id: graph_file)

    store = CommunityKuzuGraphRuntimeStore()
    monkeypatch.setattr(store, "_configured_max_bytes", lambda: 100)

    footprint = store.footprint("board-1")

    assert footprint.board_id == "board-1"
    assert footprint.status == "available"
    assert footprint.source == "runtime_capability"
    assert footprint.primary_bytes == 20
    assert footprint.sidecar_bytes == 5
    assert footprint.total_bytes == 25
    assert footprint.configured_max_bytes == 100
    assert footprint.percentage == 25.0


def test_af17_community_graph_runtime_store_footprint_reports_absent(
    tmp_path, monkeypatch
) -> None:
    graph_file = tmp_path / "missing.lbug"

    from okto_pulse.community.adapters import kg_runtime

    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda board_id: graph_file)

    store = CommunityKuzuGraphRuntimeStore()
    footprint = store.footprint("board-1")

    assert footprint.status == "unavailable"
    assert footprint.unavailable_reason == "graph_absent"


def test_af17_community_graph_runtime_store_footprint_handles_stat_error(
    tmp_path, monkeypatch
) -> None:
    class StatFailingPath:
        def exists(self) -> bool:
            return True

        def stat(self):
            raise OSError("denied")

    from okto_pulse.community.adapters import kg_runtime

    monkeypatch.setattr(
        kg_runtime,
        "board_kuzu_path",
        lambda board_id: StatFailingPath(),
    )

    footprint = CommunityKuzuGraphRuntimeStore().footprint("board-1")

    assert footprint.status == "unavailable"
    assert footprint.unavailable_reason == "stat_failed"


def test_af17_community_graph_runtime_store_purge_delegates_to_local_runtime(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_purge(board_id: str, *, reason: str) -> list[str]:
        calls.append((board_id, reason))
        return ["board.lbug", "board.lbug.wal"]

    from okto_pulse.community.adapters import kg_runtime

    monkeypatch.setattr(kg_runtime, "purge_board_graph_storage", fake_purge)

    result = CommunityKuzuGraphRuntimeStore().purge_board_graph(
        "board-1", reason="right_to_erasure"
    )

    assert calls == [("board-1", "right_to_erasure")]
    assert result.removed is True
    assert result.not_found is False
    assert result.status == "purged"
    assert result.backend == "community_local_graph"


def test_af17_community_graph_runtime_store_is_registered_as_provider() -> None:
    providers = build_community_graph_providers()

    assert isinstance(providers["graph_runtime_store"], GraphRuntimeStore)
