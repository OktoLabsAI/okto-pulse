"""Normalize current native scenario evidence through the public verifier port."""

from datetime import datetime
from dataclasses import replace

from okto_pulse.core.domain.delivery_assessment import ReadinessReason
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.domain.delivery_verification import (
    NativeVerificationPopulation, NativeVerificationRun, NativeVerificationScenario,
    VerificationObservationFact, VerificationSourceFact,
)
from okto_pulse.core.domain.verification_report import (
    parse_verification_report, require_evidence_method_binding, require_verification_report_context,
)
from okto_pulse.core.ports.test_evidence import (
    require_supported_test_verification_method, resolve_test_evidence_write_verifier,
)
from okto_pulse.core.models.schemas import TestCompanionSourceRevision
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
from okto_pulse.core.domain.verification_currentness import VerificationCurrentnessError, canonical_scenario_sha256
from okto_pulse.core.ports.verification_currentness import require_verification_currentness_store


def native_scenario_run(spec, scenario, *, verifier=None):
    """Authenticate one envelope for inspection; this alone grants no currentness."""
    return replace(_scenario_run(spec, scenario, verifier=verifier), current=False)


def _scenario_run(spec, scenario, *, verifier=None, already_authenticated=False):
    scope = DeliveryScope(spec.board_id, spec.id, int(spec.edition))
    method = scenario.get('verification_method')
    status = scenario.get('status')
    status = status if isinstance(status, str) else None
    evidence = scenario.get('evidence')
    digest = ''
    rejection = None
    reasons = []
    authenticated = False
    receipt = reference = outcome = observed_at = None
    sources, observations = (), ()
    try:
        digest = compute_test_scenario_semantic_sha256(board_id=scope.board_id,
            spec_id=scope.spec_id, scenario=scenario, acceptance_criteria=spec.acceptance_criteria or [])
    except (ValueError, TypeError, KeyError):
        rejection = ReadinessReason.VERIFICATION_RECEIPT_INVALID
        reasons.append('verification_scenario_contract_invalid')
    if evidence is None or evidence == {}:
        if status in {'passed', 'failed'}:
            rejection = ReadinessReason.VERIFICATION_RECEIPT_INVALID
            reasons.append('verification_evidence_missing')
    elif not isinstance(evidence, dict):
        rejection = ReadinessReason.VERIFICATION_RECEIPT_INVALID
        reasons.append('verification_evidence_invalid')
    else:
        receipt = evidence.get('execution_receipt')
        receipt = receipt if isinstance(receipt, str) and receipt else None
        try:
            require_supported_test_verification_method(method)
        except ValueError:
            rejection = ReadinessReason.VERIFICATION_METHOD_UNSUPPORTED
            reasons.append('verification_method_unsupported')
        try:
            require_evidence_method_binding(method, evidence)
            if evidence.get('evidence_class') == 'verification_report':
                report = parse_verification_report(evidence.get('verification_report'))
                require_verification_report_context(report, method=method, status=status,
                    criterion_ids=tuple(scenario.get('linked_criteria') or ()))
                sources = tuple(VerificationSourceFact(s.reference, s.revision, s.sha256,
                    'verification_report.sources') for s in report.sources)
                observations = tuple(VerificationObservationFact(o.observation_id, o.criterion_id,
                    o.observation_ref, o.expected, o.observed, o.outcome) for o in report.observations)
                reference, outcome, observed_at = report.report_id, report.result, report.observed_at
            else:
                attestation = evidence.get('execution_attestation') or {}
                basis = attestation.get('execution_basis') or {}
                if isinstance(basis, dict) and isinstance(basis.get('source_ref'), str) and isinstance(basis.get('revision'), str):
                    sources = (VerificationSourceFact(basis['source_ref'], basis['revision'],
                        None, 'execution_attestation.execution_basis'),)
                    companions = basis.get('companion_sources')
                    if companions is not None:
                        if not isinstance(companions, list) or len(companions) != 1:
                            raise ValueError('verification_companion_sources_invalid')
                        companion = TestCompanionSourceRevision.model_validate(companions[0])
                        if companion.source_ref == basis['source_ref']:
                            raise ValueError('verification_companion_source_duplicate')
                        sources += (VerificationSourceFact(companion.source_ref, companion.revision,
                            None, 'execution_attestation.execution_basis.companion_sources'),)
                observed_at = datetime.fromisoformat(attestation['executed_at'].replace('Z', '+00:00'))
                if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                    raise ValueError('verification_timestamp_timezone_required')
                reference = evidence.get('manifest_ref')
                reference = reference if isinstance(reference, str) else None
                outcome = status if status in {'passed', 'failed'} else None
        except (ValueError, TypeError, KeyError, AttributeError):
            rejection = rejection or ReadinessReason.VERIFICATION_RECEIPT_INVALID
            reasons.append('verification_evidence_contract_invalid')
        verifier = verifier if verifier is not None else resolve_test_evidence_write_verifier()
        if rejection is None and already_authenticated:
            authenticated = True
        elif rejection is None and verifier is not None:
            try:
                verdict = verifier.verify(board_id=scope.board_id, spec_id=scope.spec_id, status=status,
                    scenario_id=scenario['id'], scenario_sha256=digest, actor_id=None, evidence=evidence)
            except (OSError, ValueError):
                rejection = ReadinessReason.ASSESSMENT_UNAVAILABLE
                reasons.append('verification_verifier_unavailable')
            else:
                authenticated = verdict.verified
                if not authenticated:
                    rejection = ReadinessReason.VERIFICATION_RECEIPT_INVALID
                    reasons.extend(verdict.reason_codes)
        elif verifier is None and not already_authenticated:
            rejection = rejection or ReadinessReason.ASSESSMENT_UNAVAILABLE
            reasons.append('verification_verifier_unavailable')
    return NativeVerificationRun(scope, scenario['id'], digest, method if isinstance(method, str) else None,
        receipt, reference, outcome, authenticated, sources, observations, observed_at,
        rejection, tuple(reasons))


async def native_verification_population(spec, session, *, accepted_result_ids=()):
    """Read R5 authority once in the caller's SQL snapshot, never scenario pointers.

    Authentication belongs to the store. Normalization preserves the complete
    accepted basis, identity and currentness; it neither rereads receipt files
    per association nor selects historical passing results.
    """
    scenarios = spec.test_scenarios
    if not isinstance(scenarios, list) or len(scenarios) > 5000 or any(
        not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
        for row in scenarios
    ):
        return NativeVerificationPopulation((), False, ('native_verification_population_unavailable',))
    by_id = {row['id']: row for row in scenarios}
    if len(by_id) != len(scenarios):
        return NativeVerificationPopulation((), False, ('native_verification_population_unavailable',))
    try:
        currentness = await require_verification_currentness_store(session).read_current(
            board_id=spec.board_id, spec_id=spec.id, spec_edition=int(spec.edition),
            scenario_ids=tuple(by_id), accepted_result_ids=tuple(accepted_result_ids))
    except VerificationCurrentnessError as exc:
        return NativeVerificationPopulation((), False, (exc.code,))
    # Subject existence is not execution evidence. Keep the current semantic
    # identity even when reset has removed every current basis head.
    scope = DeliveryScope(spec.board_id, spec.id, int(spec.edition))
    try:
        subjects = tuple(NativeVerificationScenario(scope, scenario['id'],
            'sha256:' + canonical_scenario_sha256(compute_test_scenario_semantic_sha256(
                board_id=spec.board_id, spec_id=spec.id, scenario=scenario,
                acceptance_criteria=spec.acceptance_criteria or [])),
            scenario.get('verification_method')) for scenario in scenarios)
    except (ValueError, TypeError, KeyError):
        return NativeVerificationPopulation((), False, ('native_verification_population_unavailable',),
            currentness.source_revision)
    runs = []
    expanded = 0
    complete = currentness.complete
    reasons = set(currentness.reason_codes)
    for accepted in currentness.results:
        result = accepted.result
        context = result.context
        scenario = by_id.get(context.scenario_id)
        if scenario is None or (context.board_id, context.spec_id, context.spec_edition) != (
            spec.board_id, spec.id, int(spec.edition)):
            complete = False
            reasons.add('verification_scope_mismatch')
            continue
        row = dict(scenario, evidence=dict(accepted.evidence or {}), status=accepted.evidence_status,
            verification_method=context.verification_method)
        run = _scenario_run(spec, row, already_authenticated=True)
        valid = (run.authenticated and run.receipt_id == result.receipt_id and run.outcome == result.outcome
            and tuple(sorted((s.source_ref, s.revision) for s in run.sources)) ==
                tuple((s.source_ref, s.revision) for s in result.basis.sources))
        if accepted.current:
            valid = valid and canonical_scenario_sha256(run.scenario_sha256) == context.scenario_sha256
        if not valid and accepted.current:
            complete = False
            reasons.add('verification_accepted_result_invalid')
        run = replace(run, authenticated=bool(valid), accepted_result_id=accepted.accepted_result_id,
            current=accepted.current, basis_sha256=result.basis.digest,
            generation=context.generation, ordinal=context.ordinal,
            scenario_sha256='sha256:' + context.scenario_sha256)
        expanded += 1 + len(run.sources) + len(run.observations)
        if expanded > 20000:
            return NativeVerificationPopulation(tuple(runs), False, ('native_verification_resolution_limit',),
                currentness.source_revision, subjects)
        runs.append(run)
    return NativeVerificationPopulation(tuple(runs), complete, tuple(sorted(reasons)),
        currentness.source_revision, subjects)
