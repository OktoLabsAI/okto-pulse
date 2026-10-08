"""AC-INT-11: native operational pending work uses governed domain guidance."""
from copy import deepcopy
import json
import asyncio
import hashlib
import subprocess
from unittest.mock import Mock

import pytest
from fastmcp import Client
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import test_adopted_delivery_report as adopted
import test_delivery_evidence_integration as delivery
from test_executor_reviewer_handoff import payload
from okto_pulse.community.adapters.mcp_auth import make_community_mcp_authenticator
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, Base, Card, PermissionPreset, Spec,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.mcp import server
from okto_pulse.core.ports import McpCredential
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog
from okto_pulse.core.ports.permission_policy import (
    builtin_permission_presets, flatten_permission_flags,
    registered_permission_flags, set_permission_flag,
)
from okto_pulse.core.runtime_registry import register_unit_of_work_factory
from okto_pulse.core.services.main import AgentService

ledger = delivery.ledger


async def domain_snapshot(factory):
    # Include every relational table except authentication's last-used metadata.
    async with factory() as db:
        result = {}
        for name, table in Base.metadata.tables.items():
            columns = [column for column in table.c
                       if not (name == "agents" and column.name == "last_used_at")]
            rows = (await db.execute(select(*columns))).mappings().all()
            result[name] = sorted(
                json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["native_schema"], indirect=True)
async def test_pending_operational_remediation_is_authorized_domain_work(
    ledger, tmp_path, monkeypatch,
):
    session, _, _ = await adopted.setup(ledger, tmp_path, monkeypatch, adopted=False)
    factory = async_sessionmaker(session.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={"realm_scope": RealmScope.local()})
    try:
        spec = await session.get(Spec, delivery.SPEC_ID)
        criteria = deepcopy(spec.acceptance_criteria)
        criteria[0].update(
            text="Observe service health before accepting the operational requirement",
            verification_profile="operational",
            requirement_links=[{"requirement_type": "observability_requirement",
                                "requirement_id": "or-health"}])
        scenario = {**delivery.SCENARIO, "title": "Observe service health",
                    "verification_method": "demonstration", "status": "ready", "evidence": None}
        await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
            functional_requirements=[], acceptance_criteria=criteria,
            observability_requirements=[{
                "id": "or-health", "title": "Observe service health", "signal_type": "health",
                "linked_task_ids": ["task"],
                "verification": {"mode": "explicit", "required_profiles": ["operational"]},
                "implementation_plan": {"contributions": [
                    {"card_id": "task", "scope": "whole_requirement"}]},
            }], test_scenarios=[scenario]))
        await session.execute(update(Card).where(Card.id == "test").values(status="in_progress"))
        flags = {}
        for permission in flatten_permission_flags(registered_permission_flags()):
            set_permission_flag(flags, permission, False)
        for permission in [
            "board.read", "card.entity.read", "card.validation.read",
            "kg.operations.health.read", "kg.admin.settings_read", "code_traceability.investigation.read",
            "code_traceability.evidence.read", "code_traceability.target.read",
            "code_traceability.overlap.read",
        ]:
            set_permission_flag(flags, permission, True)
        root = next(row for row in builtin_permission_presets() if row["name"] == "Full Control")
        session.add(PermissionPreset(id="or-root", name=root["name"], flags=root["flags"], is_builtin=True))
        session.add(Agent(id="observer", name="observer", created_by="owner",
            api_key="fixture-observer", api_key_hash=AgentService.hash_api_key("fixture-observer"),
            is_active=True, preset_id="or-root", permission_flags=flags))
        session.add(AgentBoard(id="or-grant", agent_id="observer",
            board_id=delivery.BOARD_ID, granted_by="owner"))
        await session.commit()
        await session.close()
        register_unit_of_work_factory(CommunityUnitOfWorkFactory(factory))
        server.register_mcp_authenticator(make_community_mcp_authenticator(session_factory=factory))
        server._permission_cache.clear()
        monkeypatch.setattr(server, "active_api_key_credential", lambda: McpCredential(
            source="x_api_key_header", value="fixture-observer"))
        frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
        host = CommunityMcpHostProvider().materialize_catalog(
            server.mcp, resource_catalog=frozen, projection_identity=frozen.identity)
        from okto_pulse.community.adapters.sqlalchemy_kg_health import CommunitySqlAlchemyKGHealthReader
        from okto_pulse.core.ports.kg_health import register_kg_health_read_port
        from okto_pulse.core.application.rebuild_processor import RebuildProcessor
        from okto_pulse.community.adapters.sqlalchemy_queue_health import CommunitySqlAlchemyQueueHealthReader
        from okto_pulse.core.ports.queue_health import register_queue_health_read_port
        register_queue_health_read_port(CommunitySqlAlchemyQueueHealthReader())
        register_kg_health_read_port(CommunitySqlAlchemyKGHealthReader())
        launch = Mock(side_effect=AssertionError("read must not launch a process"))
        rebuild = Mock(side_effect=AssertionError("read must not rebuild"))
        monkeypatch.setattr(subprocess, "Popen", launch)
        monkeypatch.setattr(RebuildProcessor, "execute", rebuild)

        def graph_files():
            return {str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (tmp_path / "kg").rglob("*") if path.is_file()}

        before_graph = graph_files()
        before = await domain_snapshot(factory)
        async with Client(host) as client:
            context = payload(await client.call_tool("okto_pulse_get_task_context", {
                "board_id": delivery.BOARD_ID, "card_id": "test",
                "profile": "full", "context_scope": "gate",
            }, raise_on_error=False))
            flow = context["test_card_operational_flow"]
            assert not any(word in json.dumps(flow).lower() for word in ("repair", "rebuild", "start_agent"))
            assert flow["mutation_allowed"] is False
            assert flow["would_block_done"] is True
            assert flow["next_action"]["tool"] == "okto_pulse_update_test_scenario_status"
            assert flow["next_action"]["scenario_ids"] == [scenario["id"]]
            assert flow["next_action"]["follow_up"]["tool"] == "okto_pulse_move_card"
            assert await domain_snapshot(factory) == before
            refused = await client.call_tool(flow["next_action"]["tool"], {
                "board_id": delivery.BOARD_ID, "spec_id": delivery.SPEC_ID,
                "scenario_id": scenario["id"], "status": "passed",
            }, raise_on_error=False)
            assert refused.is_error, refused.content
            error = json.loads(json.loads(refused.content[0].text)["data"]["error"])
            assert error["error"] == "Permission denied"
            assert error["reason"] == "interact_in_blocked"
            assert error["required_permission"] == "test_scenario.interact_in.ready"
            assert await domain_snapshot(factory) == before
            health = payload(await client.call_tool("okto_pulse_kg_health", {
                "board_id": delivery.BOARD_ID, "profile": "full",
            }, raise_on_error=False))
            assert health["board_id"] == delivery.BOARD_ID
            assert health["operator_action"] in {"none", "inspect_telemetry"}
            from okto_pulse.core.services.kg_health_service import drain_health_probe_runtime
            assert await asyncio.to_thread(drain_health_probe_runtime, timeout_s=10.0) == 0
            assert graph_files() == before_graph
            launch.assert_not_called()
            rebuild.assert_not_called()
            assert await domain_snapshot(factory) == before
    finally:
        await session.close()
