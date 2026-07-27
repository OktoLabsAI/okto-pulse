"""REST contract coverage for governed Spec lineage failures."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response

from okto_pulse.community.api import specs as specs_api
from okto_pulse.core.application.errors import (
    ResourceLineageResolutionError,
    SpecLineagePreflightError,
)
from okto_pulse.core.models.schemas import SpecCreate, SpecUpdate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lineage_error",
    (
        ResourceLineageResolutionError(
            "Parent lineage does not resolve.",
            code="parent_lineage_mismatch",
            details={"ideation_id": "idea-1", "refinement_id": "refinement-2"},
        ),
        SpecLineagePreflightError(
            "spec_ideation_not_done",
            "A Spec can only be created from an ideation in status 'done'.",
            facts={"ideation_id": "idea-draft", "ideation_status": "draft"},
        ),
    ),
)
async def test_create_spec_maps_lineage_failures_to_typed_422(
    monkeypatch: Any,
    lineage_error: Exception,
) -> None:
    async def _raise(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise lineage_error

    monkeypatch.setattr(specs_api.CreateSpecUseCase, "execute", _raise)

    with pytest.raises(HTTPException) as raised:
        await specs_api.create_spec(
            "board-1",
            SpecCreate(title="Invalid lineage", ideation_id="idea-draft"),
            user_id="user-1",
            uow=SimpleNamespace(),
        )

    assert raised.value.status_code == 422
    assert raised.value.detail == lineage_error.to_error_dict()


@pytest.mark.asyncio
async def test_update_spec_maps_lineage_failure_to_typed_422(
    monkeypatch: Any,
) -> None:
    lineage_error = SpecLineagePreflightError(
        "spec_refinement_required",
        "A completed refinement is required.",
        facts={"ideation_id": "idea-medium", "ideation_complexity": "medium"},
    )

    async def _raise(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise lineage_error

    monkeypatch.setattr(specs_api.UpdateSpecUseCase, "execute", _raise)

    with pytest.raises(HTTPException) as raised:
        await specs_api.update_spec(
            "spec-1",
            SpecUpdate(ideation_id="idea-medium"),
            Response(),
            user_id="user-1",
            uow=SimpleNamespace(),
        )

    assert raised.value.status_code == 422
    assert raised.value.detail == lineage_error.to_error_dict()
