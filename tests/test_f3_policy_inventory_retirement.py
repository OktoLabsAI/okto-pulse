"""Native policy inventory preserves current subjects and Board isolation."""
import pytest
from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import CommunitySqlAlchemyGuidelinePolicy
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Ideation, Refinement, Spec
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from test_skb_b08_guideline_impact_persistence import _fresh_database

BOARD = "board-f3-policy-history"

@pytest.mark.asyncio
async def test_live_policy_inventory_keeps_all_other_subjects_and_board_scope(tmp_path):
    await _fresh_database(tmp_path / "live.db")
    async with get_session_factory()() as session:
        session.add_all([
            Board(id=BOARD, realm_id="local", name="Board", owner_id="owner"),
            Board(id="other-board", realm_id="local", name="Other", owner_id="owner"),
            Ideation(id="idea", board_id=BOARD, title="Idea", created_by="owner", version=2, edition=3),
            Refinement(id="refinement", board_id=BOARD, ideation_id="idea", title="Refinement",
                       created_by="owner", version=4, edition=2),
            Spec(id="spec", board_id=BOARD, title="Spec", created_by="owner", version=3, edition=2,
                 architecture_adoption=ArchitectureAdoptionScope(board_id=BOARD, spec_id="spec",
                    adopted_in_edition=2, actor_id="owner", inherited_resource_ids=()).model_dump(mode="json"),
                 execution_contract=new_execution_contract(board_id=BOARD, spec_id="spec",
                    edition=2, actor_id="owner", origin="new_spec"),
                 test_scenario_policy_epoch=7, test_scenarios=[{"id": "scenario"}]),
            Card(id="card", board_id=BOARD, title="Card", created_by="owner", policy_version=5),
            Card(id="other-card", board_id="other-board", title="Other", created_by="owner"),
        ])
        await session.flush()
        await session.commit()
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        subjects = await adapter.list_policy_subjects(board_id=BOARD)
        assert [(subject.entity_type.value, subject.subject_id, subject.subject_version, subject.subject_edition)
                for subject in subjects] == [
            ("card", "card", 5, None), ("ideation", "idea", 2, 3),
            ("refinement", "refinement", 4, 2), ("spec", "spec", 3, 2),
            ("test_scenario", "scenario", 7, None),
        ]
        assert await adapter.list_policy_subjects(board_id="absent-board") == ()
