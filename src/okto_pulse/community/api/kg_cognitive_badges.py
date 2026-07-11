"""REST endpoint for cognitive consolidation badges (KG-03.6 / api_28a22fec).

Batch read model used by first-line entity card surfaces to render the
"Pending cognitive consolidation" badge without making one HTTP request
per card.

Contract (Codex audit val_f87b8844 rework — method aligned to GET):

    GET /api/v1/kg/cognitive-pending/badges
        ?board_id=<uuid>
        &source_refs=<ref>            # repeated; 1..200 total
        &source_refs=<ref>
        ...
        &kg_generation_id=<uuid>?     # optional, latest fallback

    Response (200):
        {
          "board_id": "<uuid>",
          "selected_kg_generation_id": "<uuid>" | null,
          "readonly": true,
          "eligible_entity_types": ["bug", "decision", "refinement", "spec", "task", "test"],
          "badges": {"<ref>": {show_badge, label, status, item_id?, updated_at?, reason}}
        }

    Errors:
        - 400 invalid_source_refs (size out of [1, 200])
        - 503 cognitive_badges_unavailable

The endpoint derives the card entity type from the source_ref prefix
(``<type>:<id>``). Anything outside the eligible set collapses to
``other`` (ineligible) so the response stays deterministic without
asking the client to send a card-type mapping.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.kg.cognitive_badge_resolver import (
    BADGE_LABEL_ACTIVE,
    CognitiveBadgeReason,
    ELIGIBLE_ENTITY_TYPES,
    EntityCardType,
    emit_invalid_batch_sample,
    emit_unavailable_batch_sample,
    resolve_entity_badges,
)
from okto_pulse.core.kg.rebuild_audit import (
    CognitiveConsolidationItemStore,
    require_rebuild_audit_artifact_store,
)


router = APIRouter()


MIN_SOURCE_REFS = 1
MAX_SOURCE_REFS = 200


class CognitivePendingBadgeView(BaseModel):
    show_badge: bool
    label: str
    status: str | None = None
    item_id: str | None = None
    updated_at: str | None = None
    reason: str


class CognitivePendingBadgesResponse(BaseModel):
    board_id: str
    selected_kg_generation_id: str | None = None
    readonly: bool = True
    eligible_entity_types: list[str]
    badges: dict[str, CognitivePendingBadgeView]


def _derive_entity_type(source_ref: str) -> str:
    """Derive the card entity type from the ``<type>:<id>`` prefix.

    Unknown / malformed source_refs collapse to ``other`` so the
    resolver always sees a bounded entity-type label and never echoes
    raw payload into the metric label space (br_858a0859)."""

    if not isinstance(source_ref, str) or ":" not in source_ref:
        return EntityCardType.OTHER.value
    prefix = source_ref.split(":", 1)[0]
    known = {t.value for t in EntityCardType}
    return prefix if prefix in known else EntityCardType.OTHER.value


@router.get(
    "/kg/cognitive-pending/badges",
    response_model=CognitivePendingBadgesResponse,
    tags=["kg-cognitive-badges"],
)
async def get_cognitive_pending_badges(
    board_id: str = Query(..., min_length=1),
    source_refs: list[str] = Query(default_factory=list),
    kg_generation_id: str | None = Query(default=None),
    _user: str = Depends(require_user),
) -> CognitivePendingBadgesResponse:
    """Resolve cognitive consolidation badges for a batch of source_refs.

    GET with repeated ``source_refs`` query params keeps the surface
    cacheable and proxy-friendly while honoring the api_28a22fec
    contract (no mutation, bounded batch size)."""

    requested_count = len(source_refs)
    if requested_count < MIN_SOURCE_REFS or requested_count > MAX_SOURCE_REFS:
        emit_invalid_batch_sample(
            board_id=board_id,
            requested_count=requested_count,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_source_refs",
                "message": (
                    f"source_refs must contain {MIN_SOURCE_REFS}..{MAX_SOURCE_REFS} refs"
                ),
                "received_count": requested_count,
            },
        )

    try:
        store = CognitiveConsolidationItemStore(
            artifact_store=require_rebuild_audit_artifact_store()
        )
    except Exception as exc:
        emit_unavailable_batch_sample(
            board_id=board_id,
            requested_count=requested_count,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "cognitive_badges_unavailable",
                "message": "badge resolver could not read cognitive item store",
                "reason": type(exc).__name__,
            },
        ) from exc

    card_entity_types: dict[str, str] = {
        source_ref: _derive_entity_type(source_ref)
        for source_ref in source_refs
    }

    try:
        badges, eligible = resolve_entity_badges(
            board_id=board_id,
            source_refs=source_refs,
            card_entity_types=card_entity_types,
            kg_generation_id=kg_generation_id,
            store=store,
        )
    except Exception as exc:
        emit_unavailable_batch_sample(
            board_id=board_id,
            requested_count=requested_count,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "cognitive_badges_unavailable",
                "message": "badge resolver failed",
                "reason": type(exc).__name__,
            },
        ) from exc

    resolved_generation = (
        kg_generation_id
        if kg_generation_id
        else store.latest_generation(board_id)
    )

    badge_models: dict[str, CognitivePendingBadgeView] = {
        source_ref: CognitivePendingBadgeView(**view.to_api())
        for source_ref, view in badges.items()
    }

    return CognitivePendingBadgesResponse(
        board_id=board_id,
        selected_kg_generation_id=resolved_generation,
        readonly=True,
        eligible_entity_types=sorted(eligible),
        badges=badge_models,
    )


__all__ = [
    "BADGE_LABEL_ACTIVE",
    "CognitiveBadgeReason",
    "CognitivePendingBadgeView",
    "CognitivePendingBadgesResponse",
    "ELIGIBLE_ENTITY_TYPES",
    "MAX_SOURCE_REFS",
    "MIN_SOURCE_REFS",
    "router",
]
