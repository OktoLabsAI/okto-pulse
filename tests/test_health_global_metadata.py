"""Global metadata limits apply only to Health, never privacy/recovery readers."""

import json

from okto_pulse.community.adapters.grafx_global_discovery_runtime import CommunityGrafxGlobalDiscoveryRuntime
from okto_pulse.community.adapters.global_discovery_layout import (
    active_pointer_path, canonical_sha256, generation_graph_path,
    switch_active_generation, write_generation_manifest,
)


def _runtime(anchor, remaining=None):
    def forbidden():
        raise AssertionError("Metadata observation must not open Grafx")

    return CommunityGrafxGlobalDiscoveryRuntime(
        forbidden, lambda: anchor, forbidden, lambda _: forbidden(),
        query_timeout=remaining,
    )


def test_global_health_shares_byte_budget_between_pointer_and_manifest(tmp_path):
    anchor = tmp_path / "discovery"
    generation = "gdr_metadata_aggregate"
    graph = generation_graph_path(anchor, generation)
    graph.mkdir(parents=True)
    (graph / "grafx.meta").write_bytes(b"identity")
    digest, _ = write_generation_manifest(anchor, generation, {"history": "x" * (3 * 1024 * 1024)})
    switch_active_generation(anchor, generation_id=generation, manifest_sha256=digest)
    pointer = active_pointer_path(anchor)
    document = json.loads(pointer.read_text(encoding="utf-8"))
    del document["pointer_sha256"]
    document["history"] = "x" * (2 * 1024 * 1024)
    document["pointer_sha256"] = canonical_sha256(document)
    pointer.write_text(json.dumps(document), encoding="utf-8")

    assert _runtime(anchor).state().state.value == "present_readable_candidate"
    assert _runtime(anchor, lambda: 1).state().state.value == "present_unreadable_or_error"
    assert _runtime(anchor).state().state.value == "present_readable_candidate"


def test_global_absence_refuses_incomplete_namespace_scan(tmp_path):
    anchor = tmp_path / "discovery"
    for index in range(2001):
        (tmp_path / f"unrelated-{index}").touch()
    assert _runtime(anchor).state().state.value == "confirmed_absent"
    assert _runtime(anchor, lambda: 1).state().state.value == "present_unreadable_or_error"


def test_global_metadata_discards_result_after_final_identity_check(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import grafx_global_discovery_runtime as module

    remaining = [1.0]

    def slow_identity(_path):
        remaining[0] = 0.0
        return True

    monkeypatch.setattr(module, "has_grafx_identity", slow_identity)
    assert _runtime(tmp_path / "discovery", lambda: remaining[0]).state().state.value == "present_unreadable_or_error"
