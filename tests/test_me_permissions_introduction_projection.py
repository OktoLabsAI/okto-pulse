"""Integration contract for the permission-introduction metadata sent to UI."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from okto_pulse.community.api.me import get_my_permissions
from okto_pulse.core.application.use_cases.permission_presets import (
    GetMyPermissionsUseCase,
)
from okto_pulse.core.ports.permission_policy import (
    permission_introduction_manifests,
)


@pytest.mark.asyncio
async def test_me_permissions_projects_every_current_core_introduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permissions = SimpleNamespace(
        board_id="board-1",
        preset_name="Full Control",
        flags={},
        owner_review_required=False,
        review_reason=None,
    )

    async def execute(_self, command, *, actor, uow):
        assert command.board_id == "board-1"
        assert actor.actor_id == "user-1"
        assert uow is not None
        return SimpleNamespace(permissions=permissions)

    monkeypatch.setattr(GetMyPermissionsUseCase, "execute", execute)

    response = await get_my_permissions(
        board_id="board-1",
        user_id="user-1",
        realm_id="realm-1",
        uow=object(),
    )

    authorities = response.introduced_historical_authorities
    assert authorities["sprint.tasks.assign"] == "sprint.entity.assign"
    assert (
        authorities["ideation.move.review_to_approved"]
        == "ideation.entity.read"
    )
    assert "story.move.draft_to_ready" not in authorities
    expected_authorities = {
        leaf: authority
        for manifest in permission_introduction_manifests()
        for leaf, authority in manifest.historical_authorities
    }
    assert authorities == expected_authorities
