"""Compare individual/batch classification inside the same native Draft-to-Done flow."""
import pytest

from test_single_agent_spec_execution import (
    adopted_context as source_context,
    call,
    test_one_agent_preserves_assessments_and_evidence_through_spec_done as execute_native_flow,
)


adopted_context = source_context


async def classify_execution_context(client, scope, batch_size):
    for uri in ("okto-pulse://reference/tool-docs/architecture",):
        assert await client.read_resource(uri)
    payload = dict(board_id=scope["board_id"], parent_type="spec", parent_id=scope["spec_id"],
        title="External procedure notification contracts",
        global_description="External notifications are context, outside the procedure display delivered here.",
        entities=[{"id": "publisher", "name": "Procedure Publisher", "entity_type": "service",
                   "responsibility": "Publish procedure notifications", "boundaries": "External publisher; procedure view is read-only"},
                  {"id": "consumer", "name": "Audit Consumer", "entity_type": "service",
                   "responsibility": "Observe procedure notifications", "boundaries": "External audit consumer; no control of procedure delivery"}],
        interfaces=[{"id": "event-" + str(i), "name": "Procedure notification " + str(i),
                     "participants": ["publisher", "consumer"], "contract_type": "event", "endpoint": "procedure.notification." + str(i),
                     "source_entity_id": "publisher", "target_entity_id": "consumer",
                     "direction": "source_to_target", "protocol": "event",
                     "description": "Publisher notifies the audit consumer of a procedure.",
                     "event_schema": {"type": "object", "properties": {"procedure_id": {"type": "string"}}}}
                    for i in range(26)])
    payload["diagrams"] = [{
        "id": "context", "title": "External notifications", "diagram_type": "context",
        "format": "excalidraw_json",
        "adapter_payload": {"type": "excalidraw", "version": 2, "elements": [
            {"id": "publisher-node", "type": "rectangle", "label": "Procedure Publisher", "linkedEntityId": "publisher"},
            {"id": "consumer-node", "type": "rectangle", "label": "Audit Consumer", "linkedEntityId": "consumer"},
            {"id": "events-edge", "type": "arrow", "sourceElementId": "publisher-node",
             "targetElementId": "consumer-node", "linkedInterfaceIds": ["event-" + str(i) for i in range(26)]},
        ], "appState": {}, "files": {}},
    }]
    await call(client, "okto_pulse_get_architecture_design_schema", board_id=scope["board_id"])
    critique = await call(client, "okto_pulse_validate_architecture_design_payload", **payload)
    assert critique["valid"], critique
    assert not critique.get("structured_warnings"), critique
    created = await call(client, "okto_pulse_add_architecture_design", **payload)
    if "architecture_design" not in created:
        assert created["code"] == "architecture_warning_acknowledgement_required", created
        assert created["warning_keys"]
        created = await call(client, "okto_pulse_add_architecture_design", **payload,
            architecture_warning_acknowledgement={"accepted": True,
                "statement": "Reviewed contextual notification warnings; no diagram or implementation is claimed."})
    assert "architecture_design" in created, created
    await call(client, "okto_pulse_copy_architecture_to_card", **scope, card_id="task",
        design_ids=[created["architecture_design"]["id"]],
        architecture_warning_acknowledgement={"accepted": True,
            "statement": "Preserve the reviewed external context and its warnings on the responsible task."})
    candidates = []
    while True:
        page = await call(client, "okto_pulse_list_architecture_candidates", **scope, offset=len(candidates))
        candidates.extend(page["candidates"])
        if not page["has_more"]:
            break
    assert len(candidates) == page["total"] == 26
    version, edition = page["spec_version"], page["spec_edition"]
    for item in candidates:
        detail = await call(client, "okto_pulse_list_architecture_candidates", **scope,
                            candidate_id=item["id"], source_digest=item["source_digest"])
        assert detail["candidates"][0]["contract"]["event_schema"] == {
            "type": "object", "properties": {"procedure_id": {"type": "string"}}}
    for offset in range(0, len(candidates), batch_size):
        result = await call(client, "okto_pulse_classify_architecture_candidates", **scope, batch={
            "expected_spec_version": version, "expected_spec_edition": edition,
            "idempotency_key": "continuous-classification-" + str(offset),
            "decisions": [{"candidate_ref": item["id"], "expected_source_digest": item["source_digest"],
                           "disposition": "context_only", "reason": "External notification outside this procedure view"}
                          for item in candidates[offset:offset + batch_size]],
        })
        version = result["spec_version"]
    final = await call(client, "okto_pulse_list_architecture_classifications", **scope)
    assert final["classification_complete"] and final["state_counts"]["current"] == 26
    assert not final["admission_evaluated"] and not final["semantic_review_evaluated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted_context", ["native_schema"], indirect=True)
@pytest.mark.parametrize("batch_size", [1, 50], ids=["individual", "batch"])
@pytest.mark.parametrize("separate_review", [False, True], ids=["broad-agent", "separate-reviewer"])
async def test_continuous_native_classification_through_done(
    adopted_context, tmp_path, monkeypatch, batch_size, separate_review,
):
    await execute_native_flow(adopted_context, tmp_path, monkeypatch,
                              late_requirement_link=False, separate_review=separate_review,
                              classification_batch_size=batch_size)
