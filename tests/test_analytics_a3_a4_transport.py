from __future__ import annotations

import pytest
from fastapi import HTTPException

from okto_pulse.community.api import analytics as analytics_api
from okto_pulse.community.api.analytics_transport import (
    CanonicalCoverageResponseDTO,
    CanonicalFlowHealthResponseDTO,
    FlowHealthSettingsResponseDTO,
)
from okto_pulse.core.application.use_cases import (
    FlowHealthSettingsVersionConflict,
    PermissionDeniedError,
)
from okto_pulse.core.models.schemas import (
    FlowHealthSettingsRestore,
    FlowHealthSettingsUpdate,
)


def _canonical_coverage() -> dict[str, object]:
    identity = {
        "spec_id": "spec-1",
        "obligation_type": "ac",
        "obligation_id": "ac-1",
        "edition": 2,
        "currentness": "current",
    }
    return {
        "contract_version": "1",
        "foundation_version": "1",
        "query_fingerprint": "a" * 64,
        "filters": [{"field": "status", "operator": "in", "value": ["active"]}],
        "as_of": "2026-08-20T12:00:00.000000Z",
        "population_scope": {
            "scope_ref": "actor:user-1",
            "accessible_count": 1,
            "excluded_count": 0,
        },
        "exclusions": {
            "restricted_count": 0,
            "excluded_count": 0,
            "reasons": [],
        },
        "evidence_population_scope": {
            "scope_ref": "actor:user-1",
            "accessible_count": 7,
            "excluded_count": 0,
        },
        "evidence_exclusions": {
            "restricted_count": 0,
            "excluded_count": 0,
            "reasons": [],
        },
        "totals": {
            "state": "available",
            "applicable": 1,
            "covered": 1,
            "uncovered": 0,
            "skipped": 0,
            "value": 100.0,
            "n": 1,
            "reason": None,
        },
        "coverage": [
            {
                "obligation_type": "ac",
                "state": "available",
                "applicable": 1,
                "covered": 1,
                "uncovered": 0,
                "skipped": 0,
                "value": 100.0,
                "n": 1,
                "reason": None,
                "rows": [
                    {
                        "identity": identity,
                        "state": "covered",
                        "applicable": True,
                        "covered": True,
                        "skip": {
                            "state": "not_skipped",
                            "effective": False,
                            "authority_ref": None,
                            "reason_code": None,
                            "currentness": None,
                        },
                        "authority_ref": "spec:spec-1:ac:ac-1",
                        "reason": None,
                        "evidence": [
                            {
                                "evidence_id": "evidence-1",
                                "evidence_type": "test",
                                "source_ref": "card:card-1",
                                "obligation": identity,
                                "relation_type": "tests",
                                "evidence_content_sha256": "b" * 64,
                                "parent_card_id": "card-1",
                                "delivery_state": "active",
                                "lifecycle_status": "active",
                                "currentness": "current",
                                "currentness_reason": None,
                                "authority_ref": "evidence:evidence-1",
                                "eligibility": "eligible",
                            }
                        ],
                    }
                ],
            }
        ],
        "code_evidence": {
            "state": "available",
            "reason": None,
            "targets": [
                {
                    "target_id": "target-1",
                    "card_id": "card-1",
                    "source_ref": "card:card-1",
                    "revision": 3,
                    "lifecycle_status": "active",
                    "delivery_state": "active",
                    "currentness": "current",
                    "currentness_reason": None,
                    "current_resolution_id": "resolution-1",
                }
            ],
            "resolutions": [
                {
                    "resolution_id": "resolution-1",
                    "target_id": "target-1",
                    "target_revision": 3,
                    "state": "resolved",
                    "currentness": "current",
                    "currentness_reason": None,
                    "authority_ref": "resolution:resolution-1",
                }
            ],
            "executions": [
                {
                    "execution_id": "execution-1",
                    "target_id": "target-1",
                    "target_revision": 3,
                    "disposition": "touched",
                    "currentness": "current",
                    "currentness_reason": None,
                    "authority_ref": "execution:execution-1",
                }
            ],
            "overlaps": [
                {
                    "overlap_id": "overlap-1",
                    "target_a_id": "target-1",
                    "target_b_id": "target-2",
                    "resolution_a_id": "resolution-1",
                    "resolution_b_id": "resolution-2",
                    "severity": "medium",
                    "disposition": "accepted_parallel",
                    "currentness": "current",
                    "currentness_reason": None,
                }
            ],
            "waivers": [
                {
                    "waiver_id": "waiver-1",
                    "entity_type": "card",
                    "entity_id": "card-1",
                    "scope": "target_overlap",
                    "reason_code": "documentation_only",
                    "active": True,
                    "currentness": "current",
                    "currentness_reason": None,
                    "authority_ref": "waiver:waiver-1",
                }
            ],
        },
        "next_cursor": None,
    }


def _canonical_flow_health() -> dict[str, object]:
    return {
        "contract_version": "1",
        "foundation_version": "1",
        "query_fingerprint": "c" * 64,
        "filters": [],
        "as_of": "2026-08-20T12:00:00.000000Z",
        "effective_policy": {
            "version": 4,
            "authority_ref": "board:board-1:flow-health:v4",
            "general_stale_hours": 48,
            "rejected_stale_hours": 72,
            "overrides": [{"state": "in_progress", "stale_hours": 24}],
        },
        "population_scope": {
            "scope_ref": "actor:user-1",
            "accessible_count": 1,
            "excluded_count": 0,
        },
        "exclusions": {
            "restricted_count": 0,
            "excluded_count": 0,
            "reasons": [],
        },
        "summary": {
            "healthy": 0,
            "at_risk": 0,
            "blocked": 1,
            "stale": 0,
            "restricted": 0,
            "unavailable": 0,
            "inconsistent": 0,
        },
        "items": [
            {
                "subject": {"type": "card", "id": "card-1"},
                "state": "blocked",
                "reason_codes": ["spec_pending_validation"],
                "threshold": {
                    "state": "in_progress",
                    "stale_hours": 24,
                    "provenance": "override",
                    "policy_version": 4,
                    "authority_ref": "board:board-1:flow-health:v4",
                },
                "current_episode": {
                    "state": "in_progress",
                    "entered_at": "2026-08-19T12:00:00.000000Z",
                    "age_seconds": 86400,
                    "entry_event_id": "event-2",
                    "authority_ref": "domain-event:event-2",
                },
                "rework": [
                    {
                        "attempt": 1,
                        "rejected_at": "2026-08-18T10:00:00.000000Z",
                        "rejection_event_id": "event-1",
                        "rejection_kind": "quality",
                        "rejection_code": "tests_failed",
                        "rejection_summary": "Tests failed",
                        "resumed_at": "2026-08-18T11:00:00.000000Z",
                        "completed_at": None,
                    }
                ],
                "blockers": [
                    {
                        "code": "spec_pending_validation",
                        "authority_state": "current",
                        "authority_ref": "spec:spec-1:validation:current",
                        "effective_skip": False,
                    }
                ],
                "source_authority": {
                    "authority": "domain_events",
                    "reference": "domain-events:board-1:card:card-1",
                    "timestamp_field": "occurred_at",
                },
            }
        ],
        "next_cursor": None,
    }


def test_canonical_a3_a4_dtos_keep_every_public_core_field() -> None:
    coverage = _canonical_coverage()
    flow_health = _canonical_flow_health()

    assert (
        CanonicalCoverageResponseDTO.model_validate(coverage).model_dump(mode="json")
        == coverage
    )
    assert (
        CanonicalFlowHealthResponseDTO.model_validate(flow_health).model_dump(
            mode="json"
        )
        == flow_health
    )


def test_canonical_routes_publish_closed_response_models() -> None:
    route_models = {
        (route.path, next(iter(route.methods))): route.response_model
        for route in analytics_api.router.routes
        if getattr(route, "methods", None)
    }

    assert route_models[
        ("/boards/{board_id}/analytics/coverage/canonical", "GET")
    ] is CanonicalCoverageResponseDTO
    assert route_models[
        ("/boards/{board_id}/analytics/flow-health", "GET")
    ] is CanonicalFlowHealthResponseDTO
    assert route_models[
        ("/boards/{board_id}/analytics/flow-health/settings", "GET")
    ] is FlowHealthSettingsResponseDTO
    assert route_models[
        ("/boards/{board_id}/analytics/flow-health/settings", "PATCH")
    ] is FlowHealthSettingsResponseDTO
    assert route_models[
        ("/boards/{board_id}/analytics/flow-health/settings/restore", "POST")
    ] is FlowHealthSettingsResponseDTO


@pytest.mark.asyncio
async def test_flow_health_settings_get_patch_and_restore_use_core_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Result:
        def canonical_dict(self) -> dict[str, object]:
            return {
                "board_id": "board-1",
                "settings": {
                    "version": 3,
                    "general_stale_hours": 36,
                    "rejected_stale_hours": 60,
                    "overrides": {"in_progress": 18},
                },
            }

    async def execute(_self, command, *, actor, uow):
        assert actor.actor_id == "user-1"
        assert actor.board_id == "board-1"
        assert uow is sentinel_uow
        calls.append(command)
        return Result()

    monkeypatch.setattr(analytics_api.GetFlowHealthSettingsUseCase, "execute", execute)
    monkeypatch.setattr(analytics_api.SaveFlowHealthSettingsUseCase, "execute", execute)
    monkeypatch.setattr(
        analytics_api.RestoreFlowHealthSettingsUseCase, "execute", execute
    )
    sentinel_uow = object()
    update = FlowHealthSettingsUpdate(
        expected_version=2,
        general_stale_hours=36,
        rejected_stale_hours=60,
        overrides={"in_progress": 18},
    )
    restore = FlowHealthSettingsRestore(expected_version=2)

    read = await analytics_api.get_flow_health_settings(
        "board-1", user_id="user-1", uow=sentinel_uow
    )
    saved = await analytics_api.save_flow_health_settings(
        "board-1", update, user_id="user-1", uow=sentinel_uow
    )
    restored = await analytics_api.restore_flow_health_settings(
        "board-1", restore, user_id="user-1", uow=sentinel_uow
    )

    assert read == saved == restored
    assert calls[0].board_id == "board-1"
    assert calls[1].update is update
    assert calls[2].restore is restore


@pytest.mark.asyncio
async def test_flow_health_settings_cas_conflict_is_http_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(_self, command, *, actor, uow):
        raise FlowHealthSettingsVersionConflict(
            expected_version=2, current_version=3
        )

    monkeypatch.setattr(analytics_api.SaveFlowHealthSettingsUseCase, "execute", execute)
    update = FlowHealthSettingsUpdate(
        expected_version=2,
        general_stale_hours=48,
        rejected_stale_hours=72,
    )

    with pytest.raises(HTTPException) as captured:
        await analytics_api.save_flow_health_settings(
            "board-1", update, user_id="user-1", uow=object()
        )

    assert captured.value.status_code == 409
    assert captured.value.detail == {
        "code": "flow_health_settings_version_conflict",
        "expected_version": 2,
        "current_version": 3,
    }


@pytest.mark.asyncio
async def test_flow_health_settings_permission_denial_is_http_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(_self, command, *, actor, uow):
        raise PermissionDeniedError("board.admin.edit required")

    monkeypatch.setattr(
        analytics_api.RestoreFlowHealthSettingsUseCase, "execute", execute
    )

    with pytest.raises(HTTPException) as captured:
        await analytics_api.restore_flow_health_settings(
            "board-1",
            FlowHealthSettingsRestore(expected_version=2),
            user_id="user-1",
            uow=object(),
        )

    assert captured.value.status_code == 403
    assert captured.value.detail == "board.admin.edit required"
