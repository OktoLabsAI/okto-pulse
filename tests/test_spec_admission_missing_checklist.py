"""Real UI reproduction: missing Board binding must refuse admission, not HTTP 500."""
import pytest
import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from okto_pulse.community.api import specs
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Board, ChecklistValidationBindingSnapshotRow, DomainEventRow,
    QualityAssessmentHeadRow, QualityAssessmentLifecycleTransitionRow, Spec, SpecHistory,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.enums import SpecStatus
from test_spec_validation_lifecycle_gate import _new_spec
from test_spec_validation_native_gate import db_factory, ACTOR, USER_ID

__all__ = ['db_factory']


@pytest.mark.asyncio
async def test_unconfigured_checklist_returns_actionable_conflict_and_rolls_back(db_factory):
    board_id, spec_id = 'unconfigured-board', 'admission-spec'
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            db.add(Board(id=board_id, name='Fresh Board without configuration', owner_id=USER_ID, realm_id='local'))
            db.add(_new_spec(id=spec_id, board_id=board_id, title='UI admission reproduction',
                             status=SpecStatus.REVIEW, created_by=USER_ID, edition=1, version=1))
            await uow.commit()

    models = [ActivityLog, SpecHistory, DomainEventRow, QualityAssessmentHeadRow,
              QualityAssessmentLifecycleTransitionRow, ChecklistValidationBindingSnapshotRow]

    async def snapshot():
        async with db_factory() as db:
            row = await db.get(Spec, spec_id)
            return (row.status, row.version, row.edition, row.evaluations, row.validations,
                    [(await db.execute(select(func.count()).select_from(model))).scalar_one()
                     for model in models])

    before = await snapshot()
    app = FastAPI()
    app.include_router(specs.router, prefix='/api/v1')
    app.dependency_overrides[specs.require_user] = lambda: USER_ID

    async def uow_dependency():
        async with db_factory() as db:
            async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
                yield uow

    app.dependency_overrides[specs.get_unit_of_work] = uow_dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/api/v1/specs/{spec_id}/move', json={'status': 'approved'})
    assert response.status_code == 409, response.text
    error = response.json()['detail']
    assert error['code'] == 'checklist_board_binding_missing'
    assert error['details']['blocked_transition'] == 'approved'
    assert error['details']['next_action'] == {'action': 'configure_board_checklist', 'board_id': board_id}
    assert 'Board settings' in error['message']
    assert await snapshot() == before
