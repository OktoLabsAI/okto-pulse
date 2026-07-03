from __future__ import annotations

from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey


def test_af27_community_rebuild_audit_storage_preserves_local_layout(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-af27"

    event_key = RebuildAuditKey(
        namespace="event_audit",
        board_id=board_id,
        artifact_id="evt_1",
    )
    store.write_json_atomic(event_key, {"event_id": "evt_1"})
    assert (tmp_path / "rebuild" / "audit" / "events" / board_id / "evt_1.json").exists()

    pending_key = RebuildAuditKey(
        namespace="cognitive_pending",
        board_id=board_id,
        kg_generation_id="kg_1",
    )
    store.write_json_atomic(
        pending_key,
        {"board_id": board_id, "kg_generation_id": "kg_1", "items": []},
    )
    replaced = store.replace_json(
        pending_key,
        lambda payload: {**(payload or {}), "pending_count": 2},
    )
    assert replaced["pending_count"] == 2
    assert (
        tmp_path / "rebuild" / "audit" / "cognitive_pending" / board_id / "kg_1.json"
    ).exists()
    assert store.list_json(RebuildAuditKey("cognitive_pending", board_id))[0][
        "kg_generation_id"
    ] == "kg_1"

    confirmation_key = RebuildAuditKey(
        namespace="confirmation_audit",
        board_id=board_id,
        artifact_id="audit_1",
    )
    store.write_json_atomic(confirmation_key, {"audit_id": "audit_1"})
    assert (
        tmp_path
        / "rebuild"
        / "audit"
        / "confirmation"
        / board_id
        / "audit_1.json"
    ).exists()
