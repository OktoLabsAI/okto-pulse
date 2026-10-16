"""Native criterion links use one owner and exact Spec-local identities."""

from copy import deepcopy

import httpx
import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract

import test_verification_start_transition as verification

adopted_context = verification.adopted_context


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "foreign", "wrong_type"])
async def test_criterion_links_are_bidirectional_without_reverse_writes(adopted_context, tmp_path, invalid):
    db = adopted_context
    app, _, _ = await verification.four_profiles(db, tmp_path)
    db.add(Spec(id="criterion-foreign-spec", board_id="board", title="Other scope", created_by="author",
                architecture_adoption=ArchitectureAdoptionScope(board_id="board", spec_id="criterion-foreign-spec",
                    adopted_in_edition=1, actor_id="author", inherited_resource_ids=()).model_dump(mode="json"),
                execution_contract=new_execution_contract(board_id="board", spec_id="criterion-foreign-spec",
                    edition=1, actor_id="author", origin="new_spec"),
                functional_requirements=[{"id": "foreign-fr", "text": "Same visible wording"}]))
    await db.commit()
    spec = await db.get(Spec, "spec", populate_existing=True)
    links = [
        {"requirement_type": "functional_requirement", "requirement_id": "fr"},
        {"requirement_type": "technical_requirement", "requirement_id": "tr"},
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Establish the canonical writer representation before comparing link-only writes.
        await verification.patch_requirement(client, db, "acceptance_criterion", "ac-procedure",
                                             {"text": "All procedure steps remain visible"})
        await db.refresh(spec)
        requirements = deepcopy((spec.functional_requirements, spec.technical_requirements))
        await verification.patch_requirement(client, db, "acceptance_criterion", "ac-procedure",
                                             {"requirement_links": links})
        visible = await client.get("/api/v1/specs/spec")
        assert visible.status_code == 200, visible.text
        criterion = next(item for item in visible.json()["acceptance_criteria"] if item["id"] == "ac-procedure")
        assert [{key: item[key] for key in ("requirement_type", "requirement_id")}
                for item in criterion["requirement_links"]] == links
        for link in links:
            reverse = await client.get("/api/v1/boards/board/specs/spec/requirement-verification", params=link)
            assert reverse.status_code == 200, reverse.text
            assert len(reverse.json()["items"]) == 1
            assert "ac-procedure" in {path["criterion_id"] for path in reverse.json()["items"][0]["criteria_paths"]}
        await db.refresh(spec)
        assert (spec.functional_requirements, spec.technical_requirements) == requirements
        before = await verification.start.classification.snapshot(db)
        bad = {"requirement_type": "functional_requirement", "requirement_id": {
            "missing": "absent", "foreign": "foreign-fr", "wrong_type": "tr"}[invalid]}
        refused = await client.patch("/api/v1/specs/spec/structured-entities/acceptance_criterion/ac-procedure",
            json={"expected_spec_version": spec.version, "payload": {"requirement_links": [bad]}})
        assert refused.status_code in {400, 422}, refused.text
        assert "criterion_requirement_link_unresolved" in refused.text, refused.text
        assert await verification.start.classification.snapshot(db) == before
        await verification.patch_requirement(client, db, "acceptance_criterion", "ac-procedure",
                                             {"requirement_links": []})
        diagnostic = await client.get("/api/v1/boards/board/specs/spec/requirement-verification")
        assert diagnostic.status_code == 200, diagnostic.text
        assert not diagnostic.json()["verification_work_complete"]
        assert "criterion_requirement_link_missing" in diagnostic.text
        await db.refresh(spec)
        assert spec.status == "draft"
        assert (spec.functional_requirements, spec.technical_requirements) == requirements
