"""Attach current Core-resolved requirement choices to the native plan."""
from dataclasses import replace
from okto_pulse.community.adapters.sqlalchemy_task_requirement_decisions import CommunityTaskRequirementDecisionStore
from okto_pulse.core.domain.task_requirement_decision import resolve_task_requirement, resolve_task_requirement_allocation
from okto_pulse.core.services.task_requirement_decision import task_requirement_scenario_digest


async def apply_task_requirement_allocation(session, *, spec, cards, plan):
    histories = await CommunityTaskRequirementDecisionStore(session).history(
        board_id=spec.board_id, spec_id=spec.id, edition=int(spec.edition))
    if not histories:
        return plan
    by_id = {card['id']: card for card in cards}
    needed = {identity for key in histories if key in by_id and by_id[key]['card_type'] == 'test'
              for identity in (by_id[key].get('test_scenario_ids') or ())}
    digests = {row['id']: task_requirement_scenario_digest(board_id=spec.board_id,
        spec_id=spec.id, scenario=row, acceptance_criteria=spec.acceptance_criteria)
        for row in spec.test_scenarios or () if row.get('id') in needed}
    states, applicable = [], {}
    for identity, history in histories.items():
        # Validate the chain even when a changed/deleted allocation makes it
        # inapplicable. Never resurrect an older matching head.
        resolve_task_requirement(history[-1].scope, history)
        card = by_id.get(identity)
        if card is None or card['archived']:
            continue
        try:
            scope = plan.inventory.task_requirement_scope(board_id=spec.board_id, spec_id=spec.id,
                edition=int(spec.edition), card=card, scenario_digests=digests)
        except ValueError:
            continue  # Inapplicable history grants no exception to the new scope.
        resolution = resolve_task_requirement(scope, history)
        states.append((scope, resolution))
        if resolution.record is not None:
            applicable[identity] = history
    allocation = {row.obligation_ref: row for row in resolve_task_requirement_allocation(
        inventory=plan.inventory, board_id=spec.board_id, spec_id=spec.id, edition=int(spec.edition),
        cards=cards, histories=applicable)}
    inventory = replace(plan.inventory, rows=tuple(replace(row,
        requirement_allocation=allocation.get(row.binding.obligation_ref)) for row in plan.inventory.rows))
    return replace(plan, inventory=inventory, task_requirements=tuple(states))
