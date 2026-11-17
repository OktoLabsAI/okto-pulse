"""Reserved Path B confirmation -> exact, current native Delivery attribution.

No foreign ledger record is rewritten, and legacy confirmations retain only
their original regression eligibility. This module never commits or executes.
"""
from dataclasses import asdict, replace

from sqlalchemy import select

from okto_pulse.core.domain.delivery_evidence import AmendmentDeliveryAttribution, DeliveryBinding, DeliveryScope
from okto_pulse.core.domain.delivery_inventory import delivery_digest
from okto_pulse.core.domain.effective_delivery_coverage import amendment_source_proof
from okto_pulse.core.domain.enums import CardType
from okto_pulse.core.application.amendment_delivery import (
    current_coverage_basis,
    AmendmentLineageFact, CoverageConfirmationFact, evaluate_path_b_for_scenario,
)
from .sqlalchemy_models import AmendmentHotfixRevision, Card, Spec

CONTRACT = 'amendment-delivery-attribution/v1'


def _amendment_digest(amendment):
    return delivery_digest({field: getattr(amendment, field, None) for field in (
        'id', 'board_id', 'original_spec_id', 'origin_bug_id', 'revision_spec_id',
        'origin_task_ids', 'affected_task_ids', 'regression_scenario_ids',
        'regression_test_task_ids', 'automated_regression_refs', 'status', 'lineage_state')})


def _bug_digest(bug):
    # Include Bug-specific semantics omitted from the generic Card binding.
    return delivery_digest({field: getattr(bug, field, None) for field in (
        'id', 'board_id', 'spec_id', 'title', 'description', 'details',
        'origin_task_id', 'expected_behavior', 'observed_behavior',
        'steps_to_reproduce', 'action_plan', 'linked_test_task_ids',
    )})


def _binding(bug):
    return DeliveryBinding('card:' + bug.id, delivery_digest({
        'contract_version': 'card-delivery-scope/v2',
        'title': bug.title, 'description': bug.description, 'details': bug.details,
        'card_type': getattr(bug.card_type, 'value', bug.card_type),
    }))


async def _material(store, amendment, confirmation):
    bug = await store.session.get(Card, amendment.origin_bug_id, populate_existing=True)
    original = await store.session.get(Spec, amendment.original_spec_id, populate_existing=True)
    revision = await store.session.get(Spec, amendment.revision_spec_id, populate_existing=True) if amendment.revision_spec_id else None
    if (bug is None or original is None or revision is None
        or bug.card_type != CardType.BUG or bug.archived or original.archived or revision.archived
        or bug.board_id != amendment.board_id or original.board_id != amendment.board_id
        or revision.board_id != amendment.board_id or bug.spec_id != original.id
        or revision.id == original.id or getattr(revision.status, 'value', revision.status) != 'done'):
        return None
    cc = CoverageConfirmationFact.from_metadata(confirmation)
    if cc is None or cc.basis is None or cc.basis.scenario_spec_id != revision.id:
        return None
    # A named Test must actually belong to this exact revision, not only share
    # the Board or a similarly named scenario.
    test = await store.session.get(Card, cc.regression_test_task_id, populate_existing=True)
    origin_task = await store.session.get(Card, bug.origin_task_id, populate_existing=True) if bug.origin_task_id else None
    if (test is None or test.spec_id != revision.id or test.board_id != amendment.board_id
        or origin_task is None or origin_task.board_id != bug.board_id
        or origin_task.spec_id != original.id):
        return None
    basis = await current_coverage_basis(store.session, amendment=amendment,
        task_id=cc.regression_test_task_id, scenario_id=cc.regression_scenario_id,
        scenario_spec_id=revision.id)
    fact = replace(AmendmentLineageFact.from_row(amendment),
        coverage_confirmation=cc, current_coverage_basis=basis)
    eligibility = evaluate_path_b_for_scenario(amendment_facts=[fact],
        scenario_id=cc.regression_scenario_id,
        bug_id=bug.id, spec_id=original.id, board_id=bug.board_id,
        bug_authoritative_task_ids=frozenset((bug.origin_task_id,)))
    # A changed/reset result may remove verification without deleting already
    # accepted implementation. Ineligible lineage never grants either phase.
    if eligibility.outcome.value not in {'ready', 'coverage_pending'}:
        return None
    source, _ = await store.load_rollup_snapshot(amendment.board_id, revision.id,
        include_card_summaries=False, include_attributions=False)
    return bug, original, revision, source, eligibility.outcome.value == 'ready'


async def freeze_attribution(store, amendment, confirmation):
    material = await _material(store, amendment, confirmation)
    if material is None:
        return None
    bug, original, revision, source, covered = material
    origin, _ = await store.load_rollup_snapshot(bug.board_id, original.id,
        include_card_summaries=False, include_attributions=False)
    context = origin.effective_context
    rows = context.inventory.card_obligations(bug.id) if context else ()
    binding = _binding(bug)
    if (len(rows) != 1 or rows[0].binding != binding or rows[0].family != 'card'
        or rows[0].blockers or len(rows[0].required_contributions) != 1
        or rows[0].required_contributions[0].card_id != bug.id):
        return None
    proof = amendment_source_proof(source)
    if proof is None or not proof[3] or not proof[4] or not covered:
        return None
    # The regression artifact used by the confirmation must be part of the
    # complete proof, not an unrelated passing Test beside it.
    if not any(t.id in proof[2] and t.card_id == confirmation['regression_test_task_id']
        and t.scenario_id == confirmation['regression_scenario_id'] for t in source.tests):
        return None
    return dict(contract_version=CONTRACT, amendment_id=amendment.id,
        amendment_sha256=_amendment_digest(amendment),
        origin_scope=asdict(origin.scope), bug_id=bug.id, binding=asdict(binding),
        bug_semantic_sha256=_bug_digest(bug), revision_scope=asdict(source.scope),
        inventory_sha256=proof[0], implementation_ids=list(proof[1]), test_ids=list(proof[2]))


async def load_attributions(store, board_id, spec_id):
    amendments = list((await store.session.scalars(select(AmendmentHotfixRevision).where(
        AmendmentHotfixRevision.board_id == board_id,
        AmendmentHotfixRevision.original_spec_id == spec_id).order_by(AmendmentHotfixRevision.id).limit(101)
        .execution_options(populate_existing=True))).all())
    if len(amendments) > 100:
        raise ValueError('amendment_delivery_population_limit')
    result = []
    for amendment in amendments:
        metadata = amendment.validation_metadata
        confirmation = metadata.get('coverage_confirmation') if isinstance(metadata, dict) else None
        frozen = confirmation.get('delivery_attribution') if isinstance(confirmation, dict) else None
        if not isinstance(frozen, dict) or frozen.get('contract_version') != CONTRACT:
            continue  # Legacy Path B coverage has no Delivery authority.
        material = await _material(store, amendment, confirmation)
        if material is None:
            continue
        bug, original, revision, source, covered = material
        scope = DeliveryScope(board_id, spec_id, int(original.edition))
        binding = _binding(bug)
        if (frozen.get('amendment_id') != amendment.id or frozen.get('bug_id') != bug.id
            or frozen.get('amendment_sha256') != _amendment_digest(amendment)
            or frozen.get('origin_scope') != asdict(scope)
            or frozen.get('binding') != asdict(binding)
            or frozen.get('bug_semantic_sha256') != _bug_digest(bug)
            or frozen.get('revision_scope') != asdict(source.scope)):
            continue
        ids = [frozen.get(name) for name in ('implementation_ids', 'test_ids')]
        if any(not isinstance(values, list) or not values or len(values) > 10000
            or any(not isinstance(v, str) or not v for v in values)
            or len(set(values)) != len(values) for values in ids):
            continue
        result.append(AmendmentDeliveryAttribution(amendment.id, scope, bug.id,
            binding, bug.status, source.scope, frozen.get('inventory_sha256'),
            tuple(ids[0]), tuple(ids[1]), source, True, covered))
    return tuple(result)
