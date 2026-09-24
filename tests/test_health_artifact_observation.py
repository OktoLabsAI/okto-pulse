"""Health observes persisted cognitive evidence without storage maintenance."""

import json
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.core.kg import rebuild_audit
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey, RebuildAuditObservationBudget
from okto_pulse.core.services.kg_health_service import _read_cognitive_health_counts, _read_current_kg_generation


@pytest.fixture
def observation(tmp_path, monkeypatch):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(namespace="cognitive_pending", board_id="board", kg_generation_id="generation")
    store.write_json_atomic(key, {
        "board_id": "board", "kg_generation_id": "generation",
        "recorded_at": "2026-09-24T00:00:00Z", "pending_refs": ["spec:one"],
    })
    monkeypatch.setattr(rebuild_audit, "require_rebuild_audit_artifact_store", lambda: store)
    return store, tmp_path / "rebuild/audit/cognitive_pending/board/generation.json"


def test_health_does_not_clean_orphan_temporary_artifacts(observation):
    _store, path = observation
    temporary = path.with_name(f".{path.name}.orphan.tmp")
    temporary.write_bytes(b"unfinished writer evidence")
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    for _ in range(2):
        assert _read_cognitive_health_counts("board") == (1, 0, "available")
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before


def test_corrupt_cognitive_artifact_is_unavailable_not_empty(observation):
    _store, path = observation
    path.write_text("{ incomplete", encoding="utf-8")
    assert _read_cognitive_health_counts("board")[2] == "unavailable"
    assert path.read_text(encoding="utf-8") == "{ incomplete"


@pytest.mark.parametrize("damage", ["array", "missing_generation", "unknown_status", "invalid_refs"])
def test_invalid_cognitive_evidence_does_not_become_zero(observation, damage):
    _store, path = observation
    record = json.loads(path.read_text(encoding="utf-8"))
    if damage == "array":
        record = []
    elif damage == "missing_generation":
        del record["kg_generation_id"]
    elif damage == "unknown_status":
        record["items"] = [{"status": "invented"}]
    else:
        record["pending_refs"] = "spec:one"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert _read_cognitive_health_counts("board")[2] == "unavailable"


@pytest.mark.parametrize("budget", [
    RebuildAuditObservationBudget(max_records=1),
    RebuildAuditObservationBudget(max_entries=1),
    RebuildAuditObservationBudget(max_bytes=1),
])
def test_observation_budget_refuses_without_partial_result_or_cleanup(observation, budget):
    store, path = observation
    path.with_name("another.json").write_bytes(path.read_bytes())
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    with pytest.raises(ValueError, match="rebuild_observation_.*_limit"):
        store.observe_health_json(RebuildAuditKey(namespace="cognitive_pending", board_id="board"), budget=budget)
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before


def test_observation_aggregate_bytes_and_no_unbounded_fallback(observation, monkeypatch):
    store, path = observation
    monkeypatch.setattr(store, "read_json", lambda *_: pytest.fail("ordinary read"))
    monkeypatch.setattr(store, "list_json", lambda *_: pytest.fail("ordinary enumeration"))
    assert _read_cognitive_health_counts("board") == (1, 0, "available")
    path.with_name("another.json").write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="byte_limit"):
        store.observe_health_json(
            RebuildAuditKey(namespace="cognitive_pending", board_id="board"),
            budget=RebuildAuditObservationBudget(max_bytes=path.stat().st_size + 1),
        )


def test_health_generation_observation_preserves_temporary_files_and_detects_invalid_pointer(observation, monkeypatch):
    store, cognitive = observation
    from okto_pulse.core.kg import interfaces

    monkeypatch.setattr(interfaces, "get_kg_registry", lambda: SimpleNamespace(require_rebuild_audit_artifact_store=lambda: store))
    generation = "675c43ee-7d91-4cc3-8f87-44eeb293f90c"
    key = RebuildAuditKey(namespace="generation_current", board_id="board", artifact_id="current")
    store.write_json_atomic(key, {"kg_generation_id": generation})
    path = cognitive.parents[4] / "rebuild/generations/board/current.json"
    temporary = path.with_name(".current.json.orphan.tmp")
    temporary.write_bytes(b"pending writer")
    assert _read_current_kg_generation("board") == (generation, "available", "ok")
    assert temporary.read_bytes() == b"pending writer"
    path.write_text('{"kg_generation_id": "invalid"}', encoding="utf-8")
    assert _read_current_kg_generation("board")[1] == "unavailable"


def test_absent_observation_does_not_create_a_board_directory(observation):
    store, path = observation
    assert _read_cognitive_health_counts("absent") == (0, 0, "available")
    assert not (path.parent.parent / "absent").exists()


def test_latest_generation_order_and_item_interpretation_are_preserved(observation):
    store, path = observation
    first = json.loads(path.read_text(encoding="utf-8"))
    second = dict(first, kg_generation_id="z-latest", items=[
        {"status": "pending"}, {"status": "failed"}, {"status": "in_progress"},
        {"status": "consolidated"}, {"status": "skipped"},
    ])
    store.write_json_atomic(RebuildAuditKey(namespace="cognitive_pending", board_id="board", kg_generation_id="z-latest"), second)
    assert _read_cognitive_health_counts("board") == (3, 0, "available")


def test_expired_observation_stops_before_json_decoding(observation, monkeypatch):
    from okto_pulse.community.adapters import rebuild_audit_storage as adapter

    store, _path = observation
    clock = iter([0.0, 1.0])
    monkeypatch.setattr(adapter, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(adapter.json, "loads", lambda *_: pytest.fail("expired observation decoded JSON"))
    with pytest.raises(TimeoutError, match="rebuild_observation_timeout"):
        store.observe_health_json(
            RebuildAuditKey(namespace="cognitive_pending", board_id="board"),
            budget=RebuildAuditObservationBudget(),
        )


def test_artifact_changed_during_observation_is_not_a_complete_snapshot(observation, monkeypatch):
    from okto_pulse.community.adapters import rebuild_audit_storage as adapter

    store, path = observation
    decode = json.loads

    def change_during_decode(encoded):
        value = decode(encoded)
        path.write_bytes(encoded + b" ")
        return value

    monkeypatch.setattr(adapter.json, "loads", change_during_decode)
    with pytest.raises(ValueError, match="rebuild_observation_changed"):
        store.observe_health_json(
            RebuildAuditKey(namespace="cognitive_pending", board_id="board"),
            budget=RebuildAuditObservationBudget(),
        )
