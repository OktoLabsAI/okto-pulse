"""Authenticated delivery on the current contract, including replay and recovery.

Uses this checkout's current fixtures and a disposable SQLite database. This
segment does not qualify the full initiative or establish comparative savings.
"""

import json
from pathlib import Path
import sys

import pytest
from fastmcp import Client
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from test_delivery_evidence_integration import (  # noqa: E402
    BOARD_ID,
    SPEC_ID,
    command,
    ledger as source_ledger,
)
from okto_pulse.community.adapters.mcp_auth import make_community_mcp_authenticator
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent,
    AgentBoard,
    CardDeliveryEvidenceRecordRow,
    PermissionPreset,
    Spec,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWorkFactory,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    configure_community_database,
    build_community_session_factory,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.infra.config import configure_settings
from okto_pulse.core.mcp import server
from okto_pulse.core.ports import McpCredential
from okto_pulse.core.ports.mcp_resources import freeze_mcp_resource_catalog
from okto_pulse.core.ports.permission_policy import builtin_permission_presets
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
)
from okto_pulse.core.runtime_context import RuntimeValueRegistry, runtime_value_scope
from okto_pulse.core.runtime_registry import (
    register_relational_runtime_factory,
    register_unit_of_work_factory,
)
from okto_pulse.core.services.main import AgentService

ledger = source_ledger


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path):
    with runtime_value_scope(RuntimeValueRegistry()):
        register_relational_runtime_factory(
            lambda url, echo=False: configure_community_database(url, echo=echo)
        )
        register_relational_application_adapter(CommunityRelationalApplicationAdapter())
        configure_settings(
            CommunitySettings(
                data_dir=str(tmp_path / "data"),
                database_url="sqlite+aiosqlite:///"
                + (tmp_path / "runtime.db").as_posix(),
                upload_dir=str(tmp_path / "uploads"),
                kg_base_dir=str(tmp_path / "kg"),
            )
        )
        yield


def body(result):
    assert not result.is_error, result.content
    value = json.loads(result.content[0].text)
    assert value.get("outcome") != "error", value
    return value["data"] if "outcome" in value else value


@pytest.mark.asyncio
async def test_native_authenticated_delivery_record_replay_and_new_session(
    ledger, monkeypatch
):
    seed, _, _ = ledger
    factory = build_community_session_factory(seed.bind)
    preset = next(
        p for p in builtin_permission_presets() if p["name"] == "Full Control"
    )
    seed.add(
        PermissionPreset(
            id="benchmark-full",
            name=preset["name"],
            flags=preset["flags"],
            is_builtin=True,
        )
    )
    seed.add(
        Agent(
            id="agent-1",
            name="Benchmark agent",
            created_by="owner",
            api_key="fixture-benchmark",
            api_key_hash=AgentService.hash_api_key("fixture-benchmark"),
            is_active=True,
            permissions=[],
            permission_flags={},
            preset_id="benchmark-full",
        )
    )
    seed.add(
        AgentBoard(
            id="benchmark-board-grant",
            agent_id="agent-1",
            board_id=BOARD_ID,
            granted_by="owner",
        )
    )
    await seed.commit()
    await seed.close()
    register_unit_of_work_factory(CommunityUnitOfWorkFactory(factory))
    server.register_mcp_authenticator(
        make_community_mcp_authenticator(session_factory=factory)
    )
    server._permission_cache.clear()
    monkeypatch.setattr(
        server,
        "active_api_key_credential",
        lambda: McpCredential(source="x_api_key_header", value="fixture-benchmark"),
    )
    frozen = freeze_mcp_resource_catalog(server.effective_resource_catalog())
    host = CommunityMcpHostProvider().materialize_catalog(
        server.mcp, resource_catalog=frozen, projection_identity=frozen.identity
    )
    scope = {"board_id": BOARD_ID, "spec_id": SPEC_ID}

    async def record(client, data):
        return body(
            await client.call_tool(
                "okto_pulse_record_delivery_evidence",
                {
                    **scope,
                    "card_id": data.card_id,
                    "evidence": data.model_dump(
                        mode="json",
                        exclude_none=True,
                        exclude={"board_id", "spec_id", "card_id"},
                    ),
                },
                raise_on_error=False,
            )
        )

    async with Client(host) as client:
        resolved = await server._get_agent_ctx(BOARD_ID)
        assert resolved.agent_id == "agent-1"
        resource = await client.read_resource(
            "okto-pulse://reference/code-traceability"
        )
        assert resource
        before = body(await client.call_tool("okto_pulse_get_delivery_evidence", scope))
        assert before["allowed"] is False
        implementation = await record(client, command())
        replayed = await record(client, command())
        assert replayed["id"] == implementation["id"] and replayed["replayed"] is True

    # Deliberate session interruption after an accepted implementation binding.
    # The next session rereads source data, not the prior client's in-memory view.
    async with Client(host) as client:
        resource = await client.read_resource(
            "okto-pulse://reference/code-traceability"
        )
        assert resource
        recovered = body(
            await client.call_tool("okto_pulse_get_delivery_evidence", scope)
        )
        assert recovered["allowed"] is False
        assert any(
            implementation["id"] in row["implementation_ids"]
            for row in recovered["rows"]
        )
        tested = await record(
            client, command("test", implementation_ids=[implementation["id"]])
        )
        after = body(await client.call_tool("okto_pulse_get_delivery_evidence", scope))
        assert after["allowed"] is True
        assert any(tested["id"] in row["test_ids"] for row in after["rows"])
    async with factory() as reader:
        records = list(await reader.scalars(select(CardDeliveryEvidenceRecordRow)))
        assert len(records) == 2
        assert {r.actor_id for r in records} == {"agent-1"}
        assert {r.kind for r in records} == {"implementation", "test"}
        status = (await reader.get(Spec, SPEC_ID)).status
        assert getattr(status, "value", status) == "in_progress"
