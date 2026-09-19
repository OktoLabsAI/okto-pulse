"""Prospective selection through real derivation and persisted canonical lineage.

The earlier legacy reproduction is retained in commit aea2d9e and the ledger.
Legacy NULL scopes remain covered by test_architecture_candidates_integration.
"""

import pytest
from sqlalchemy import func, select, update

from okto_pulse.community.adapters.sqlalchemy_models import ArchitectureDesign, Card, Spec
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
from okto_pulse.core.application.use_cases.mcp_spec_crud import McpDeriveSpecCommand, McpDeriveSpecUseCase
from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
from okto_pulse.core.services.architecture_candidates import load_spec_architecture_candidates
from okto_pulse.core.services.resource_gate import ResourceGateService
from okto_pulse.core.services.resource_lineage import ResolvedResourceLineageService

from test_spec_b_knowledge_propagation_e2e import (
    ACTOR_ID, BOARD_ID, REST_ACTOR, _seed_refinement_sources,
    spec_b_runtime as spec_b_runtime,
)


def _source(design_id):
    return ArchitectureDesign(
        id=design_id, board_id=BOARD_ID, parent_type="refinement",
        refinement_id="selected-refinement", title=design_id,
        global_description="A client sends an order to the service.",
        created_by=ACTOR_ID,
        entities=[
            {"id": "client", "name": "Client", "entity_type": "web_app", "responsibility": "Send orders."},
            {"id": "service", "name": "Service", "entity_type": "api", "responsibility": "Handle orders."},
        ],
        interfaces=[{
            "id": "orders", "name": "Create order", "endpoint": "POST /orders",
            "description": "Client sends an order to the service.",
            "direction": "source_to_target", "protocol": "REST", "contract_type": "OpenAPI",
            "source_entity_id": "client", "target_entity_id": "service",
        }],
        diagrams=[{
            "id": "context", "title": "Runtime", "diagram_type": "context", "format": "excalidraw_json",
            "adapter_payload": {"type": "excalidraw", "version": 2, "elements": [
                {"id": "client-node", "type": "rectangle", "label": "Client", "linkedEntityId": "client"},
                {"id": "service-node", "type": "rectangle", "label": "Service", "linkedEntityId": "service"},
                {"id": "orders-edge", "type": "arrow", "sourceElementId": "client-node",
                 "targetElementId": "service-node", "linkedInterfaceIds": ["orders"]},
            ], "appState": {}, "files": {}},
        }],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection,mode,expected_copies,expected_roots", [
    (["chosen"], "copy", 1, {"chosen"}), ([], "copy", 0, set()),
    (["chosen"], "reference_only", 0, {"chosen"}),
    (None, "copy", 2, {"chosen", "not-chosen"}),
    (None, "none", 0, {"chosen", "not-chosen"}),
    ([], "reference_only", 0, set()),
    (["architecture:chosen"], "copy", 1, {"chosen"}),
    ([" chosen ", "chosen"], "copy", 1, {"chosen"}),
])
async def test_new_derive_persists_selection_for_candidates_coverage_and_card_context(
    spec_b_runtime, selection, mode, expected_copies, expected_roots,
):
    runtime = spec_b_runtime
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    await _seed_refinement_sources(
        runtime, refinement_id="selected-refinement", roots=(),
        architecture_designs=(_source("chosen"), _source("not-chosen")),
    )
    async with runtime.uow_factory(actor=REST_ACTOR) as uow:
        result = await McpDeriveSpecUseCase().execute(
            McpDeriveSpecCommand(
                "refinement", "selected-refinement", architecture_design_ids=selection,
                architecture_propagation_mode=mode,
            ), actor=REST_ACTOR, uow=uow,
        )
        spec_id = result.spec.id
        summary = result.resource_propagation["by_type"]["architecture"]
        assert summary["requested_ids"] == (selection or [])
        assert summary["copied"] == expected_copies

    # Reopen after commit: transient write-result attachments cannot establish
    # durable adoption authority for subsequent reads or the future start gate.
    async with runtime.sessions() as db:
        spec = await db.get(Spec, spec_id)
        assert spec.architecture_adoption["inherited_resource_ids"] == sorted(
            f"architecture:{root}" for root in expected_roots
        )
        assert spec.architecture_adoption["actor_id"] == ACTOR_ID
        assert not spec.integration_requirements
        assert await db.scalar(select(func.count()).select_from(Card)) == 0
        copies = (await db.scalars(select(ArchitectureDesign).where(
            ArchitectureDesign.spec_id == spec_id,
        ))).all()
        assert len(copies) == expected_copies
        if expected_copies == 1:
            assert copies[0].source_design_id == "chosen"
        population = await load_spec_architecture_candidates(db, board_id=BOARD_ID, spec_id=spec_id)
        assert population.resolved
        assert {candidate.root_design_id for candidate in population.candidates} == expected_roots
        resolver = ResolvedResourceLineageService(ResourceGateService(db))
        lineage = await resolver.resolve(BOARD_ID, "spec", spec_id, projection_profile="gate")
        assert {item.revision_stamp.root_id for item in lineage.coverage_obligations
                if item.resource_type == "architecture"} == expected_roots
        summary = await ResourceGateService(db).get_summary(BOARD_ID, "spec", spec_id, metadata_only=True)
        architecture = next(item for item in summary["resources"] if item["resource_type"] == "architecture")
        assert architecture["state"] == ("provided" if expected_roots else "missing")
        # Unselected sources remain in lineage as history, never as obligations.
        assert {item.revision_stamp.root_id for item in lineage.attachments
                if item.resource_type == "architecture" and not item.effective} == {"chosen", "not-chosen"} - expected_roots
        db.add(Card(id="consumer", board_id=BOARD_ID, spec_id=spec_id,
                    title="Consumer", created_by=ACTOR_ID))
        await db.commit()
        card_lineage = await resolver.resolve(BOARD_ID, "card", "consumer", projection_profile="gate")
        assert {item.revision_stamp.root_id for item in card_lineage.attachments
                if item.resource_type == "architecture" and item.effective} == expected_roots
        # Sources added later are not silently adopted; existing snapshot copies
        # stay frozen, while reference_only keeps its established live semantics.
        db.add(_source("later-source"))
        changed = _source("chosen").interfaces
        changed[0]["request_schema"] = {"const": "changed-source"}
        await db.execute(update(ArchitectureDesign).where(ArchitectureDesign.id == "chosen").values(
            interfaces=changed, version=2,
        ))
        await db.commit()
        after = await load_spec_architecture_candidates(db, board_id=BOARD_ID, spec_id=spec_id)
        assert {item.root_design_id for item in after.candidates} == expected_roots
        if "chosen" in expected_roots:
            old = next(item for item in population.candidates if item.root_design_id == "chosen")
            current = next(item for item in after.candidates if item.root_design_id == "chosen")
            assert (old.source_digest == current.source_digest) is (mode == "copy")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["copy", "reference_only", "none"])
async def test_unknown_selection_does_not_create_spec_or_copy(spec_b_runtime, mode):
    runtime = spec_b_runtime
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    await _seed_refinement_sources(
        runtime, refinement_id="selected-refinement", roots=(), architecture_designs=(_source("chosen"),),
    )
    with pytest.raises(ValueError):
        async with runtime.uow_factory(actor=REST_ACTOR) as uow:
            await McpDeriveSpecUseCase().execute(
                McpDeriveSpecCommand("refinement", "selected-refinement",
                                     architecture_design_ids=["foreign"], architecture_propagation_mode=mode),
                actor=REST_ACTOR, uow=uow,
            )
    async with runtime.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Spec)) == 0
        assert await db.scalar(select(func.count()).select_from(ArchitectureDesign)) == 1


@pytest.mark.asyncio
async def test_architecture_creation_and_read_do_not_gate_on_unrelated_resource_availability(
    spec_b_runtime, monkeypatch,
):
    runtime = spec_b_runtime
    await _seed_refinement_sources(
        runtime, refinement_id="selected-refinement", roots=(), architecture_designs=(_source("chosen"),),
    )

    async def unavailable(*args, **kwargs):
        raise AssertionError("architecture-only reads must not consult KB/mockup availability")

    for method in ("_knowledge_refs_metadata", "_mockup_refs_metadata", "_knowledge_scope_metadata"):
        monkeypatch.setattr(CommunitySqlAlchemyResourceGateAdapter, method, unavailable)
    async with runtime.uow_factory(actor=REST_ACTOR) as uow:
        result = await McpDeriveSpecUseCase().execute(
            McpDeriveSpecCommand("refinement", "selected-refinement", architecture_design_ids=["chosen"]),
            actor=REST_ACTOR, uow=uow,
        )
    async with runtime.sessions() as db:
        population = await load_spec_architecture_candidates(db, board_id=BOARD_ID, spec_id=result.spec.id)
        assert population.resolved
        assert {item.root_design_id for item in population.candidates} == {"chosen"}
