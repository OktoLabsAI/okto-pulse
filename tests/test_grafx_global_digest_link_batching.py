from __future__ import annotations

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.community.adapters.grafx_global_discovery import ensure_current_grafx_global_schema
from okto_pulse.community.adapters.grafx_global_discovery_runtime import CommunityGrafxGlobalDiscoveryRuntime


def _runtime(db, tmp_path, fence=lambda phase: None):
    return CommunityGrafxGlobalDiscoveryRuntime(
        lambda: db, lambda: tmp_path / "global", lambda: None, fence,
    )


def _seed(db, ids):
    with db.begin("write") as tx:
        tx.execute("CREATE (:Board {board_id:'board'})")
        tx.execute("CREATE (:Board {board_id:'other'})")
        for digest_id in ids:
            tx.execute(
                "CREATE (:DecisionDigest {id:$digest_id,board_id:'board'})",
                {"digest_id": digest_id},
            )
        tx.execute(
            "MATCH (b:Board {board_id:'board'}), (d:DecisionDigest) "
            "CREATE (b)-[:CONTAINS_DECISION]->(d)",
        )


def _links(db):
    return db.execute(
        "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
        "RETURN b.board_id,d.id ORDER BY b.board_id,d.id",
    ).rows


def test_full_expected_set_above_native_limit_keeps_all_valid_chunks(tmp_path):
    with connect(tmp_path / "global") as db:
        ensure_current_grafx_global_schema(db)
        _seed(db, ("digest-0", "digest-512", "digest-1024", "digest-2413", "absent", "wrong"))
        with db.begin("write") as tx:
            tx.execute("MATCH (d:DecisionDigest {id:'wrong'}) SET d.board_id='other'")
            tx.execute("MATCH (b:Board {board_id:'other'}),(d:DecisionDigest {id:'absent'}) CREATE (b)-[:CONTAINS_DECISION]->(d)")
            tx.execute("MATCH (b:Board {board_id:'board'}),(d:DecisionDigest {id:'absent'}) CREATE (b)-[:CONTAINS_DECISION]->(d)")
        expected = tuple(f"digest-{i}" for i in range(2414)) + ("wrong",)
        runtime = _runtime(db, tmp_path)
        assert runtime.delete_invalid_board_digest_links(board_id="board", expected_digest_ids=expected) == 3
        assert _links(db) == (
            ("board", "digest-0"), ("board", "digest-1024"),
            ("board", "digest-2413"), ("board", "digest-512"), ("other", "absent"),
        )
        assert runtime.delete_invalid_board_digest_links(board_id="board", expected_digest_ids=expected) == 0
        assert db.execute("MATCH (d:DecisionDigest) RETURN count(d)").rows == ((6,),)
        assert db.verify("all").findings == ()


def test_second_delete_chunk_failure_rolls_back_every_link(tmp_path):
    with connect(tmp_path / "global") as db:
        ensure_current_grafx_global_schema(db)
        _seed(db, tuple(f"invalid-{i}" for i in range(513)))
        before = _links(db)
        deletes = 0

        def fence(phase):
            nonlocal deletes
            if phase == "delete_invalid_digest_links":
                deletes += 1
                if deletes == 2:
                    raise RuntimeError("injected second chunk refusal")

        with pytest.raises(GraphError):
            _runtime(db, tmp_path, fence).delete_invalid_board_digest_links(
                board_id="board", expected_digest_ids=(),
            )
        assert deletes == 2
        assert _links(db) == before
        assert db.verify("all").findings == ()


def test_inventory_budget_refuses_before_any_delete(tmp_path):
    with connect(tmp_path / "global", max_result_rows=2) as db:
        ensure_current_grafx_global_schema(db)
        _seed(db, ("a", "b", "c"))
        phases = []
        with pytest.raises(GraphError):
            _runtime(db, tmp_path, phases.append).delete_invalid_board_digest_links(
                board_id="board", expected_digest_ids=(),
            )
        assert "delete_invalid_digest_links" not in phases
        assert db.execute("MATCH (:Board)-[r:CONTAINS_DECISION]->(:DecisionDigest) RETURN count(r)").rows == ((3,),)
