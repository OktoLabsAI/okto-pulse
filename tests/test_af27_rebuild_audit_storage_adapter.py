from __future__ import annotations

from pathlib import Path

from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
    default_community_rebuild_base_dir,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
from okto_pulse.core.kg.rebuild_generation import (
    RebuildAuditKGGenerationRepository,
)


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


def test_af16_community_generation_storage_preserves_legacy_layout(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    repo = RebuildAuditKGGenerationRepository(artifact_store=store)
    board_id = "board-af16"
    generation_id = "11111111-1111-4111-8111-111111111111"

    result = repo.promote_current(
        board_id=board_id,
        previous_kg_generation_id=None,
        kg_generation_id=generation_id,
        report_ref="rebuild-report:/board-af16/run-1",
        status="completed",
        structural_hash="structural-hash",
        source_hash="source-hash",
        promoted_by="test",
        run_id="run-1",
    )

    assert result.outcome == "promoted"
    assert result.current_kg_generation_id == generation_id
    assert repo.get_current(board_id) == generation_id
    history = repo.load_history(board_id, generation_id)
    assert history is not None
    assert history["kg_generation_id"] == generation_id
    assert (
        tmp_path / "rebuild" / "generations" / board_id / "current.json"
    ).exists()
    assert (
        tmp_path
        / "rebuild"
        / "generations"
        / board_id
        / "history"
        / f"{generation_id}.json"
    ).exists()


def test_af16_community_rebuild_base_dir_override_lives_in_adapter(
    tmp_path, monkeypatch
):
    configured = tmp_path / "configured-rebuild-root"
    monkeypatch.setenv("OKTO_PULSE_REBUILD_BASE_DIR", str(configured))

    assert default_community_rebuild_base_dir() == configured
    assert configured.exists()


def test_af16_composition_uses_community_rebuild_base_dir_resolver():
    composition_source = Path("src/okto_pulse/community/adapters/composition.py")
    source = composition_source.read_text(encoding="utf-8")
    forbidden_import = (
        "from okto_pulse.core.kg."
        + "rebuild_audit import "
        + "default_"
        + "rebuild_base_dir"
    )

    assert "default_community_rebuild_base_dir" in source
    assert forbidden_import not in source
