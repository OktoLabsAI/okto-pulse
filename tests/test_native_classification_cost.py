"""Comparable native classification segments, with public authorship and resume."""
import json

import pytest
from fastmcp import Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import test_architecture_candidates_integration as sources
import test_architecture_classification_use_case as classification
from test_single_agent_spec_execution import call
from okto_pulse.community.adapters.mcp_auth import make_community_mcp_authenticator
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, PermissionPreset, ArchitectureCandidateDecisionRow, Ideation, Spec,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.mcp import server
from okto_pulse.core.ports import McpCredential
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog
from okto_pulse.core.ports.permission_policy import builtin_permission_presets, set_permission_flag
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.services.main import AgentService

adopted_context = sources.adopted_context


@pytest.mark.asyncio
@pytest.mark.parametrize("adopted_context", ["native_schema"], indirect=True)
@pytest.mark.parametrize("read_only", [False, True], ids=["broad-reader", "read-only-reader"])
@pytest.mark.parametrize("batch_size", [1, 50], ids=["individual", "batch"])
async def test_native_authorship_classification_and_second_agent_resume(
    adopted_context, tmp_path, monkeypatch, read_only, batch_size,
):
    db = adopted_context
    # Existing completed initiative is the controlled starting boundary.
    (await db.get(Ideation, "idea")).status = "done"
    from types import SimpleNamespace
    from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
    from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
    from okto_pulse.core.ports.relational_application import register_relational_application_adapter
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    register_community_relational_effects(settings=SimpleNamespace(
        data_dir=str(tmp_path), port=1, environment="test"))
    classification.register_structured_spec_store(classification.CommunitySqlAlchemyStructuredSpecStore())
    classification.register_domain_event_publisher(classification.CommunitySqlAlchemyDomainEventPublisher())
    factory = async_sessionmaker(db.bind, expire_on_commit=False, sync_session_class=CommunitySemanticSession,
                                 info={"realm_scope": RealmScope.local()})
    from okto_pulse.community.adapters.composition import configure_community_kg_registry
    from okto_pulse.community.config import CommunitySettings
    configure_community_kg_registry(factory, settings=CommunitySettings(
        data_dir=str(tmp_path / "runtime"), kg_base_dir=str(tmp_path / "kg"),
        kg_embedding_mode="stub", kg_embedding_dim=8))
    preset = next(row for row in builtin_permission_presets() if row["name"] == "Full Control")
    db.add(PermissionPreset(id="cost-preset", name="Cost fixture", flags=preset["flags"], is_builtin=False))
    for name in ("first", "second"):
        key = "fixture-cost-" + name
        flags = {}
        if read_only and name == "second":
            set_permission_flag(flags, "spec.entity.edit_fields", False)
        db.add(Agent(id=name, name=name, created_by="author", api_key=key,
                     api_key_hash=AgentService.hash_api_key(key), is_active=True,
                     preset_id="cost-preset", permission_flags=flags))
        db.add(AgentBoard(id=name + "-board", agent_id=name, board_id="board", granted_by="author"))
    await db.commit()
    await db.close()
    register_unit_of_work_factory(CommunityUnitOfWorkFactory(factory))
    server.register_mcp_authenticator(make_community_mcp_authenticator(session_factory=factory))
    server._permission_cache.clear()
    identity = "first"
    monkeypatch.setattr(server, "active_api_key_credential", lambda: McpCredential(
        source="x_api_key_header", value="fixture-cost-" + identity))
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(
        server.mcp, resource_catalog=frozen, projection_identity=frozen.identity)
    scenarios = []

    async def protocol(client):
        for uri in ("okto-pulse://workflows/preflight", "okto-pulse://workflows/specs",
                    "okto-pulse://reference/tool-docs/architecture"):
            assert await client.read_resource(uri)

    async with Client(host) as client:
        await protocol(client)
        assert (await server._get_agent_ctx("board")).agent_id == "first"
        for count in (1, 26):
            authored = await call(client, "okto_pulse_create_spec", board_id="board",
                                  title="Classification population " + str(count),
                                  ideation_id="idea", delivery_context="greenfield")
            assert "spec" in authored, authored
            scope = {"board_id": "board", "spec_id": authored["spec"]["id"]}
            payload = dict(board_id="board",
                       parent_type="spec", parent_id=scope["spec_id"], title="Order event contracts",
                       global_description="External order notifications; contextual interfaces outside this delivery.",
                       entities=[{"id": "publisher", "name": "Order Publisher", "entity_type": "service",
                                  "responsibility": "Publish order notifications"},
                                 {"id": "consumer", "name": "Audit Consumer", "entity_type": "service",
                                  "responsibility": "Observe order notifications"}],
                       interfaces=[{"id": "event-" + str(i), "name": "Order notification " + str(i),
                                    "participants": ["publisher", "consumer"], "contract_type": "event",
                                    "event_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}}}
                                   for i in range(count)])
            await call(client, "okto_pulse_get_architecture_design_schema", board_id="board")
            critique = await call(client, "okto_pulse_validate_architecture_design_payload", **payload)
            assert critique["valid"], critique
            created = await call(client, "okto_pulse_add_architecture_design", **payload)
            if "architecture_design" not in created:
                assert created["code"] == "architecture_warning_acknowledgement_required", created
                assert created["warning_keys"]
                # Native whole-payload acknowledgment: new Design IDs scope finding keys.
                # Preserve warnings/audit; no propagation or lifecycle gate is waived.
                created = await call(client, "okto_pulse_add_architecture_design", **payload,
                                     architecture_warning_acknowledgement={
                                         "accepted": True,
                                         "statement": "Reviewed contextual notification design warnings; no diagram is claimed."})
            assert "architecture_design" in created, created
            items = []
            while True:
                page = await call(client, "okto_pulse_list_architecture_candidates", **scope, offset=len(items))
                items.extend(page["candidates"])
                if not page["has_more"]:
                    break
            assert len(items) == page["total"] == count
            version, edition = page["spec_version"], page["spec_edition"]
            # Both paths inspect exactly the same complete contracts before deciding.
            for item in items:
                detail = await call(client, "okto_pulse_list_architecture_candidates", **scope,
                                    candidate_id=item["id"], source_digest=item["source_digest"])
                assert detail["candidates"][0]["contract"]["event_schema"]["type"] == "object"
            for offset in range(0, count, batch_size):
                result = await call(client, "okto_pulse_classify_architecture_candidates", **scope, batch={
                    "expected_spec_version": version, "expected_spec_edition": edition,
                    "idempotency_key": "classification-" + str(offset),
                    "decisions": [{"candidate_ref": item["id"], "expected_source_digest": item["source_digest"],
                                   "disposition": "context_only", "reason": "External observation outside this delivery"}
                                  for item in items[offset:offset + batch_size]],
                })
                version = result["spec_version"]
            scenarios.append({"scope": scope, "count": count})

    identity = "second"
    async with Client(host) as client:
        await protocol(client)
        assert (await server._get_agent_ctx("board")).agent_id == "second"
        for scenario in scenarios:
            resumed = await call(client, "okto_pulse_list_architecture_classifications", **scenario["scope"])
            assert resumed["classification_complete"]
            assert resumed["state_counts"]["current"] == scenario["count"]
            assert not resumed["admission_evaluated"] and not resumed["semantic_review_evaluated"]
            scenario["resumed"] = resumed
        if read_only:
            refused = await client.call_tool("okto_pulse_classify_architecture_candidates", {
                **scope, "batch": {
                    "expected_spec_version": version, "expected_spec_edition": edition,
                    "idempotency_key": "reader-must-not-write",
                    "decisions": [{"candidate_ref": items[0]["id"],
                                   "expected_source_digest": items[0]["source_digest"],
                                   "disposition": "context_only", "reason": "Reader has no authorship"}],
                }}, raise_on_error=False)
            assert refused.structured_content["error_code"] == "permission_denied", refused
    async with factory() as session:
        decisions = list(await session.scalars(select(ArchitectureCandidateDecisionRow)))
        assert len(decisions) == 27
        for scenario in scenarios:
            authored_spec = await session.get(Spec, scenario["scope"]["spec_id"])
            assert authored_spec.ideation_id == "idea" and authored_spec.created_by == "first"
            scoped = [row for row in decisions if row.spec_id == scenario["scope"]["spec_id"]]
            assert len(scoped) == scenario["count"]
            assert {row.payload["interface_id"] for row in scoped} == {
                "event-" + str(i) for i in range(scenario["count"])}
            assert all(row.payload["actor_id"] == "first" and row.payload["disposition"] == "context_only"
                       and row.payload["integration_requirement_ids"] == [] for row in scoped)
            scenario["semantic_decisions"] = sorted([
                {key: row.payload[key] for key in ("interface_id", "source_contract_json", "actor_id",
                                                  "disposition", "integration_requirement_ids", "reason")}
                for row in scoped], key=lambda row: row["interface_id"])
    (tmp_path / "classification-outcome.json").write_text(json.dumps({
        "batch_size": batch_size, "read_only": read_only, "scenarios": scenarios,
    }, indent=2, default=str), encoding="utf-8")
