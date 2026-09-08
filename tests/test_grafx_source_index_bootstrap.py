"""Existing-board activation and fail-closed fresh bootstrap for source indexes."""
import okto_grafx
import pytest
from okto_grafx import Timestamp
from okto_pulse.community.adapters import grafx_schema_bootstrap as bootstrap
from okto_pulse.community.adapters.grafx_source_indexes import GrafxSourceIndexResult
from okto_pulse.core.kg.interfaces.graph_errors import GraphError


def test_existing_board_adds_missing_source_indexes_without_restamping(tmp_path, monkeypatch):
    with okto_grafx.connect(tmp_path / "graph") as db:
        ensure = bootstrap.ensure_pulse_grafx_source_indexes
        monkeypatch.setattr(bootstrap, "ensure_pulse_grafx_source_indexes",
                            lambda *_, **__: GrafxSourceIndexResult((), ()))
        bootstrap.ensure_current_grafx_board_schema(db, board_id="b", bootstrapped_at=Timestamp(micros=1))
        before = bootstrap._read_board_meta(db, table_exists=True)
        monkeypatch.setattr(bootstrap, "ensure_pulse_grafx_source_indexes", ensure)
        result = bootstrap.ensure_current_grafx_board_schema(db, board_id="b", bootstrapped_at=Timestamp(micros=2))
        assert result.changed
        assert bootstrap._read_board_meta(db, table_exists=True) == before
        assert len([i for i in db.indexes.indexes() if i.name.startswith("pulse_source_")]) == 11
        wal = db.wal
        assert not bootstrap.ensure_current_grafx_board_schema(
            db, board_id="b", bootstrapped_at=Timestamp(micros=3),
        ).changed
        assert db.wal == wal


def test_source_index_fence_failure_prevents_fresh_board_stamp_and_can_retry(tmp_path):
    with okto_grafx.connect(tmp_path / "graph") as db:
        def fence(phase):
            if phase.startswith("source_index:"):
                raise RuntimeError("source index fence lost")

        with pytest.raises(GraphError):
            bootstrap.ensure_current_grafx_board_schema(
                db, board_id="b", bootstrapped_at=Timestamp(micros=1), revalidate_fence=fence,
            )
        assert bootstrap._read_board_meta(db, table_exists=True) is None
        assert not any(i.name.startswith("pulse_source_") for i in db.indexes.indexes())
        assert bootstrap.ensure_current_grafx_board_schema(
            db, board_id="b", bootstrapped_at=Timestamp(micros=2),
        ).changed
        assert bootstrap._read_board_meta(db, table_exists=True) is not None
