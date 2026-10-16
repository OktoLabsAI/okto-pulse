"""Normalize current native scenario evidence through the public verifier port."""

from datetime import datetime

from okto_pulse.core.domain.delivery_assessment import ReadinessReason
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.domain.delivery_verification import (
    NativeVerificationPopulation, NativeVerificationRun, VerificationObservationFact, VerificationSourceFact,
)
from okto_pulse.core.domain.verification_report import (
    parse_verification_report, require_evidence_method_binding, require_verification_report_context,
)
from okto_pulse.core.ports.test_evidence import (
    require_supported_test_verification_method, resolve_test_evidence_write_verifier,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256


def native_scenario_run(spec, scenario, *, verifier=None):
    """The Spec's native evidence pointer is authoritative, never ledger order."""
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
        if rejection is None and verifier is not None:
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
        elif verifier is None:
            rejection = rejection or ReadinessReason.ASSESSMENT_UNAVAILABLE
            reasons.append('verification_verifier_unavailable')
    return NativeVerificationRun(scope, scenario['id'], digest, method if isinstance(method, str) else None,
        receipt, reference, outcome, authenticated, sources, observations, observed_at,
        rejection, tuple(reasons))


def native_verification_population(spec):
    """Read every current head once, including failures with no delivery binding."""
    scenarios = spec.test_scenarios
    if not isinstance(scenarios, list) or len(scenarios) > 5000 or any(
        not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
        for row in scenarios
    ):
        return NativeVerificationPopulation((), False, ('native_verification_population_unavailable',))
    verifier = resolve_test_evidence_write_verifier()
    runs = []
    expanded = 0
    for row in scenarios:
        run = native_scenario_run(spec, row, verifier=verifier)
        expanded += 1 + len(run.sources) + len(run.observations)
        if expanded > 20000:
            return NativeVerificationPopulation(tuple(runs), False, ('native_verification_resolution_limit',))
        runs.append(run)
    return NativeVerificationPopulation(tuple(runs), True)
