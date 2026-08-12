"""Main API router combining all sub-routers."""

from fastapi import APIRouter

from okto_pulse.community.api.boards import router as boards_router
from okto_pulse.community.api.cards import router as cards_router
from okto_pulse.community.api.checklists import router as checklists_router
from okto_pulse.community.api.quality_assessments import (
    router as quality_assessments_router,
)
from okto_pulse.community.api.agents import router as agents_router
from okto_pulse.community.api.attachments import router as attachments_router
from okto_pulse.community.api.qa import router as qa_router
from okto_pulse.community.api.comments import router as comments_router
from okto_pulse.community.api.ideations import router as ideations_router
from okto_pulse.community.api.refinements import router as refinements_router
from okto_pulse.community.api.guidelines import router as guidelines_router
from okto_pulse.community.api.policy_governance import (
    router as policy_governance_router,
)
from okto_pulse.community.api.specs import router as specs_router
from okto_pulse.community.api.stories import router as stories_router
from okto_pulse.community.api.analytics import router as analytics_router
from okto_pulse.community.api.allowed_transitions import (
    router as allowed_transitions_router,
)
from okto_pulse.community.api.architecture import router as architecture_router
from okto_pulse.community.api.presets import router as presets_router
from okto_pulse.community.api.sprints import router as sprints_router
from okto_pulse.community.api.kg_routes import router as kg_router
from okto_pulse.community.api.me import router as me_router
from okto_pulse.community.api.discovery import router as discovery_router
from okto_pulse.community.api.settings import router as settings_router
from okto_pulse.community.api.queue_health import router as queue_health_router
from okto_pulse.community.api.kg_health import router as kg_health_router
from okto_pulse.community.api.kg_rebuild import router as kg_rebuild_router
from okto_pulse.community.api.kg_canonical_debt import (
    router as kg_canonical_debt_router,
)
from okto_pulse.community.api.kg_canonical_partition_integrity import (
    router as kg_canonical_partition_integrity_router,
)
from okto_pulse.community.api.kg_digest_layer_mismatch import (
    router as kg_digest_layer_mismatch_router,
)
from okto_pulse.community.api.kg_stale_canonical_parity import (
    router as kg_stale_canonical_parity_router,
)
from okto_pulse.community.api.kg_orphan_integrity import (
    router as kg_orphan_integrity_router,
)
from okto_pulse.community.api.kg_cognitive_pending import (
    router as kg_cognitive_pending_router,
)
from okto_pulse.community.api.bug_cognitive_closure import (
    router as bug_cognitive_closure_router,
)
from okto_pulse.community.api.amendment_revisions import (
    router as amendment_revisions_router,
)
from okto_pulse.community.api.default_board_config import (
    router as default_board_config_router,
)
from okto_pulse.community.api.design_systems import router as design_systems_router
from okto_pulse.community.api.screen_mockups import router as screen_mockups_router
from okto_pulse.community.api.cognitive_action_center import (
    router as cognitive_action_center_router,
)
from okto_pulse.community.api.kg_cognitive_candidates import (
    router as kg_cognitive_candidates_router,
)
from okto_pulse.community.api.kg_cognitive_candidate_commands import (
    router as kg_cognitive_candidate_commands_router,
)
from okto_pulse.community.api.kg_cognitive_badges import (
    router as kg_cognitive_badges_router,
)
from okto_pulse.community.api.kg_tick import router as kg_tick_router
from okto_pulse.community.api.dead_letter import router as dead_letter_router
from okto_pulse.community.api.traceability import router as traceability_router
from okto_pulse.community.api.code_traceability import (
    router as code_traceability_router,
)
from okto_pulse.community.api.validation_cycles import (
    router as validation_cycles_router,
)
from okto_pulse.community.api.resource_gate import router as resource_gate_router

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(boards_router, prefix="/boards", tags=["boards"])
api_router.include_router(cards_router, prefix="/cards", tags=["cards"])
api_router.include_router(ideations_router, tags=["ideations"])
api_router.include_router(stories_router, tags=["stories"])
api_router.include_router(refinements_router, tags=["refinements"])
api_router.include_router(specs_router, tags=["specs"])
api_router.include_router(checklists_router, tags=["checklists"])
api_router.include_router(quality_assessments_router, tags=["quality-assessments"])
api_router.include_router(validation_cycles_router, tags=["validation-cycles"])
api_router.include_router(allowed_transitions_router, tags=["allowed-transitions"])
# `default_board_config_router` MUST be registered before `guidelines_router`: it owns
# the literal GET /guidelines/default-candidates, which would otherwise be shadowed by
# the parametric GET /guidelines/{guideline_id} in guidelines_router (FastAPI/Starlette
# match routes in registration order, so the param route would swallow the literal path
# and return 404 "Guideline not found"). Regression: test_guidelines_route_order.py.
api_router.include_router(default_board_config_router, tags=["default-board-config"])
# Board-scoped policy governance owns literal import/export and action routes.
# Keep it before the historical guideline router: Starlette route matching is
# registration-ordered and `/guidelines/{guideline_id}` must never capture a
# governance literal.
api_router.include_router(policy_governance_router, tags=["policy-governance"])
api_router.include_router(guidelines_router, tags=["guidelines"])
api_router.include_router(agents_router, prefix="/agents", tags=["agents"])
api_router.include_router(
    attachments_router, prefix="/attachments", tags=["attachments"]
)
api_router.include_router(qa_router, prefix="/qa", tags=["qa"])
api_router.include_router(comments_router, prefix="/comments", tags=["comments"])
api_router.include_router(analytics_router, tags=["analytics"])
api_router.include_router(architecture_router, tags=["architecture"])
api_router.include_router(presets_router, prefix="/presets", tags=["presets"])
api_router.include_router(sprints_router, tags=["sprints"])
api_router.include_router(kg_router, tags=["knowledge-graph"])
api_router.include_router(me_router, tags=["me"])
api_router.include_router(discovery_router, tags=["discovery"])
api_router.include_router(settings_router, tags=["settings"])
api_router.include_router(queue_health_router, tags=["queue-health"])
api_router.include_router(kg_health_router, tags=["kg-health"])
api_router.include_router(kg_rebuild_router, tags=["kg-rebuild"])
api_router.include_router(kg_canonical_debt_router, tags=["kg-canonical-debt"])
api_router.include_router(
    kg_canonical_partition_integrity_router,
    tags=["kg-canonical-partition-integrity"],
)
api_router.include_router(
    kg_digest_layer_mismatch_router,
    tags=["kg-digest-layer-mismatch"],
)
api_router.include_router(
    kg_stale_canonical_parity_router,
    tags=["kg-stale-canonical-parity"],
)
api_router.include_router(kg_orphan_integrity_router, tags=["kg-orphan-integrity"])
api_router.include_router(kg_cognitive_pending_router, tags=["kg-cognitive-pending"])
api_router.include_router(bug_cognitive_closure_router, tags=["bug-cognitive-closure"])
api_router.include_router(amendment_revisions_router, tags=["amendment-revisions"])
# NOTE: default_board_config_router is registered earlier (before guidelines_router)
# so its literal /guidelines/default-candidates route is not shadowed. See above.
api_router.include_router(design_systems_router, tags=["design-systems"])
api_router.include_router(screen_mockups_router, tags=["screen-mockups"])
api_router.include_router(
    cognitive_action_center_router, tags=["kg-cognitive-action-center"]
)
api_router.include_router(kg_cognitive_candidates_router, tags=["kg-cognitive-pending"])
api_router.include_router(
    kg_cognitive_candidate_commands_router, tags=["kg-cognitive-pending"]
)
api_router.include_router(kg_cognitive_badges_router, tags=["kg-cognitive-badges"])
api_router.include_router(kg_tick_router, tags=["kg-tick"])
api_router.include_router(dead_letter_router, tags=["dead-letter"])
api_router.include_router(traceability_router, tags=["traceability"])
api_router.include_router(code_traceability_router)
api_router.include_router(resource_gate_router, tags=["resource-gate"])
