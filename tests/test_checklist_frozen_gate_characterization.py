"""Regression for the authorized edition-frozen Checklist gate."""

import pytest

from okto_pulse.community.adapters.sqlalchemy_checklist import CommunitySqlAlchemyChecklist
from okto_pulse.core.domain.checklist import ChecklistMode, ChecklistPhase, ChecklistTargetType
from okto_pulse.core.services.checklist import ChecklistService, ChecklistPortContractError
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from test_sqlalchemy_checklist import BOARD_ID, SPEC_ID, session

__all__ = ["session"]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,new,allowed,reason", [
    (ChecklistMode.BLOCKING, ChecklistMode.OFF, False, "checklist_receipt_required"),
    (ChecklistMode.OFF, ChecklistMode.BLOCKING, True, "checklist_off"),
    (ChecklistMode.ADVISORY, ChecklistMode.BLOCKING, True, "checklist_advisory"),
])
async def test_board_change_cannot_replace_current_edition_governance(session, initial, new, allowed, reason):
    adapter = CommunitySqlAlchemyChecklist(session)
    service = ChecklistService()
    blocking = service.prepare_binding(
        board_id=BOARD_ID, mode=initial, current_binding=None,
    )
    await service.apply_binding(blocking, previous_binding=None, persistence=adapter)
    identity = dict(board_id=BOARD_ID, spec_id=SPEC_ID, spec_edition=1,
                    target_type=ChecklistTargetType.SPEC, phase=ChecklistPhase.SPEC_VALIDATION)
    frozen = await adapter.freeze_validation_binding(**identity)
    off = service.prepare_binding(board_id=BOARD_ID, mode=new,
                                  current_binding=blocking)
    await service.apply_binding(off, previous_binding=blocking, persistence=adapter)
    assert (await adapter.get_validation_binding(**identity)) == frozen
    assert frozen.mode is initial
    assert await adapter.get_current(board_id=BOARD_ID, spec_id=SPEC_ID,
                                     spec_edition=1, phase=ChecklistPhase.SPEC_VALIDATION) is None
    observed = await service.evaluate_spec_gate(
        board_id=BOARD_ID, spec_id=SPEC_ID, persistence=adapter,
    )
    assert observed.mode is initial
    assert observed.allowed is allowed
    assert observed.reason == reason
    spec = await session.get(Spec, SPEC_ID)
    spec.edition = 2
    spec.version += 1
    await session.flush()
    # A new edition is not admitted merely because the live Board has policy.
    with pytest.raises(ChecklistPortContractError, match="snapshot_missing"):
        await service.evaluate_spec_gate(board_id=BOARD_ID, spec_id=SPEC_ID, persistence=adapter)
    identity["spec_edition"] = 2
    await adapter.freeze_validation_binding(**identity)
    current = await service.evaluate_spec_gate(board_id=BOARD_ID, spec_id=SPEC_ID, persistence=adapter)
    assert current.mode is new
    assert current.allowed is (new is not ChecklistMode.BLOCKING)
    identity["spec_edition"] = 1
    assert await adapter.get_validation_binding(**identity) == frozen
