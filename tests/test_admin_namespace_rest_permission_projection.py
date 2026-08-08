"""REST adapters preserve Core authorization decisions for admin namespaces."""

from __future__ import annotations

import inspect
import json

import pytest
from fastapi import HTTPException

from okto_pulse.community.api import agents, boards, design_systems, presets, settings
from okto_pulse.core.application.use_cases import PermissionDeniedError


class _DeniedUseCase:
    async def execute(self, command, *, actor, uow):
        del command, actor, uow
        raise PermissionDeniedError(
            json.dumps(
                {
                    "error": "permission_denied",
                    "required_permission": "namespace.action",
                }
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execute_authorized",
    (
        agents._execute_authorized,
        presets._execute_authorized,
        design_systems._execute_authorized,
    ),
)
async def test_shared_adapter_projection_returns_structured_403(
    execute_authorized,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await execute_authorized(
            _DeniedUseCase(),
            object(),
            actor=object(),
            uow=object(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {
        "error": "permission_denied",
        "required_permission": "namespace.action",
    }


@pytest.mark.parametrize(
    "endpoint",
    (
        agents.create_agent,
        agents.list_my_agents,
        agents.list_agents_for_board,
        agents.get_agent,
        agents.update_agent,
        agents.regenerate_agent_key,
        agents.delete_agent,
        agents.grant_board_access,
        agents.update_board_overrides,
        agents.revoke_board_access,
        presets.list_presets,
        presets.create_preset,
        presets.export_presets,
        presets.export_preset,
        presets.import_presets,
        presets.clone_preset,
        presets.update_preset,
        presets.delete_preset,
        design_systems.create_design_system,
        design_systems.list_design_systems,
        design_systems.export_design_systems,
        design_systems.import_design_systems,
        design_systems.export_design_system,
        design_systems.get_design_system,
        design_systems.update_design_system,
        design_systems.delete_design_system,
        design_systems.link_board_design_system,
        design_systems.unlink_board_design_system,
        design_systems.get_board_design_system,
    ),
)
def test_admin_endpoint_cannot_bypass_core_denial_projection(endpoint) -> None:
    assert "_execute_authorized(" in inspect.getsource(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    (
        settings.get_runtime,
        settings.put_runtime,
        boards.create_board,
        boards.update_board,
        boards.delete_board,
        boards.archive_tree,
        boards.restore_tree,
        boards.share_board,
        boards.list_board_shares,
        boards.update_board_share,
        boards.revoke_board_share,
    ),
)
def test_explicit_rest_projection_handles_core_permission_denial(endpoint) -> None:
    source = inspect.getsource(endpoint)
    assert "except PermissionDeniedError" in source
    assert "permission_denied_http_error" in source
