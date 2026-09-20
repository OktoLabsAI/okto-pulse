"""F3 transport: removed Sprint names are not resurrected by FastMCP."""

from types import SimpleNamespace

import pytest
from fastmcp import Client

from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.core.mcp import server
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog


@pytest.mark.asyncio
async def test_sprint_commands_are_unknown_on_the_materialized_transport(monkeypatch):
    def no_unit_of_work(*args, **kwargs):
        pytest.fail("retired Sprint operation reached a unit of work")

    async def context(_board_id):
        return SimpleNamespace(agent_id="a", agent_name="a", permissions=["*"])

    monkeypatch.setattr(server, "get_unit_of_work_factory_for_mcp", no_unit_of_work)
    monkeypatch.setattr(server, "_get_agent_ctx", context)
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(server.mcp,
        resource_catalog=frozen, projection_identity=frozen.identity)
    async with Client(host) as client:
        listed = {tool.name for tool in await client.list_tools()}
        assert listed == {tool.name for tool in server.mcp.iter_tools()}
        assert not any("sprint" in name for name in listed)
        for suffix in ("create_sprint", "update_sprint", "move_sprint", "get_sprint",
                "get_sprint_context", "assign_tasks_to_sprint", "submit_sprint_evaluation",
                "list_sprint_evaluations", "get_sprint_evaluation", "delete_sprint_evaluation",
                "ask_sprint_question", "answer_sprint_question", "delete_sprint_question",
                "suggest_sprints"):
            result = await client.call_tool(f"okto_pulse_{suffix}",
                {"board_id": "board", "sprint_id": "old-id"}, raise_on_error=False)
            assert result.is_error
            assert "Unknown tool" in " ".join(getattr(item, "text", "") for item in result.content)
        refused = await client.call_tool("okto_pulse_ask",
            {"board_id": "board", "target_type": "sprint", "parent_id": "old-id", "question": "Q"},
            raise_on_error=False)
        assert refused.is_error
        assert "unsupported_target_type" in str(refused.structured_content)


@pytest.mark.asyncio
async def test_polymorphic_reads_cannot_expose_sprint_through_transport(monkeypatch):
    def no_lookup(*args, **kwargs):
        pytest.fail("retired read reached authentication or persistence")

    monkeypatch.setattr(server, "_get_agent_ctx", no_lookup)
    monkeypatch.setattr(server, "get_unit_of_work_factory_for_mcp", no_lookup)
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(server.mcp,
        resource_catalog=frozen, projection_identity=frozen.identity)
    async with Client(host) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}
        schema = listed["okto_pulse_list_by_board"].inputSchema
        assert schema["properties"]["entity_type"]["enum"] == [
            "spec", "ideation", "refinement", "story", "topic"]
        for name, arguments in (
            ("okto_pulse_list_by_board", {"filters": {"spec_id": "old-spec"}}),
            ("okto_pulse_get_allowed_transitions", {"entity_id": "old-id"}),
            ("okto_pulse_get_allowed_transitions", {"current_status": "draft"}),
        ):
            result = await client.call_tool(name,
                {"board_id": "board", "entity_type": "sprint", **arguments},
                raise_on_error=False)
            assert result.is_error
            assert not (result.structured_content or {}).get("data", {}).get("items")
