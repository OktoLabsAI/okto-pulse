/** Presentation of Core reason codes only; does not evaluate or authorize delivery. */
export const taskDeliveryReasons: Record<string, { reason: string; next: string }> = {
  scope_unavailable: { reason: 'The assigned scope could not be read.', next: 'Restore access to the task and its requirements, then refresh.' },
  scope_allocation_unresolved: { reason: 'Responsibility for this item is not clearly assigned.', next: 'Review which task is responsible for this requirement.' },
  scope_contract_changed: { reason: 'The requirement or assigned work changed after it was assessed.', next: 'Ask the responsible agent to check the updated scope and refresh its evidence.' },
  implementation_missing: { reason: 'There is no complete implementation evidence for this item.', next: 'The responsible agent needs to complete the work and record its implementation evidence.' },
  implementation_partial: { reason: 'Only part of the required implementation is confirmed.', next: 'Complete the remaining work and have the responsible agent update its evidence.' },
  implementation_association_missing: { reason: 'Implementation evidence exists but is not linked to this delivery.', next: 'The responsible agent needs to link the existing evidence to this item.' },
  implementation_proof_revoked: { reason: 'The implementation evidence was withdrawn.', next: 'Have the responsible agent inspect the work and record valid replacement evidence.' },
  implementation_proof_stale: { reason: 'The implementation evidence no longer matches the current work.', next: 'Have the responsible agent recheck the affected implementation.' },
  material_progress_unresolved: { reason: 'A recorded change has not yet been checked against the implementation.', next: 'Finish or recheck the recorded change before relying on earlier evidence.' },
  verification_plan_missing: { reason: 'The required verification has not been defined.', next: 'Define the check that will confirm this requirement.' },
  verification_not_run: { reason: 'There is no valid execution of the required check.', next: 'Have the responsible agent run the check and record the actual result.' },
  verification_failed: { reason: 'The required verification failed.', next: 'Review the failed check, fix its cause, then repeat verification and record the result.' },
  verification_inconclusive: { reason: 'The recorded result is insufficient to confirm verification.', next: 'Complete the missing observations or required review.' },
  verification_conflict: { reason: 'The current verification results disagree.', next: 'Review and reconcile the conflicting results.' },
  verification_association_missing: { reason: 'A valid verification result exists but is not linked to this delivery.', next: 'The responsible agent needs to link the result to the implementation it verified.' },
  verification_base_mismatch: { reason: 'Verification checked a different version of the implementation.', next: 'Link a valid result for the current version or repeat verification against it.' },
  verification_base_unresolved: { reason: 'The implementation version used for verification could not be confirmed.', next: 'Have the responsible agent identify and confirm the version that was checked.' },
  verification_receipt_invalid: { reason: 'The recorded verification cannot be accepted for this delivery.', next: 'The responsible agent needs to correct the verification record through the test or review workflow.' },
  verification_method_unsupported: { reason: 'The required verification method is unavailable.', next: 'Restore the verification method or request an authorized decision.' },
  verification_dependency_pending: { reason: 'Verification is waiting for the required implementation or version.', next: 'Resolve the implementation checks first, then run the required verification.' },
  decision_verification_pending: { reason: 'The design decision still requires evidence or review.', next: 'Complete the evidence and inspections required by the decision.' },
  applicable_obligation_waived: { reason: 'An authorized waiver applies. It does not prove implementation or verification.', next: 'Review the waiver if the requirement needs to become mandatory again.' },
  lifecycle_card_open: { reason: 'Evidence is available, but the task has not been completed.', next: 'Review the remaining completion checks before moving the task to Done.' },
  lifecycle_gate_pending: { reason: 'A task completion check is still blocking delivery.', next: 'Review the task completion requirements and resolve the outstanding check.' },
  lifecycle_transition_required: { reason: 'The current task status does not allow direct completion.', next: 'Follow the available status transition and its requirements.' },
  assessment_unavailable: { reason: 'The available information is insufficient to assess this check.', next: 'Restore the missing information or access, then refresh the task.' },
  historical_completion_diverged: { reason: 'The previously completed task is not confirmed by its current assessment.', next: 'Review the changed evidence and current completion requirements.' },
};

export const taskDeliveryActions: Record<string, string> = {
  resolve_scope: 'Review the assigned scope and responsibility.',
  implement_contribution: 'The responsible agent needs to complete the work and record its implementation evidence.',
  complete_contribution: 'Complete the remaining work and record evidence of completion.',
  associate_implementation: 'The responsible agent needs to link the existing implementation evidence.',
  refresh_implementation_proof: 'Have the responsible agent recheck the affected implementation.',
  resolve_material_change: 'Finish or recheck the recorded change.',
  execute_verification: 'Have the responsible agent run the required check against the current implementation and record the result.',
  associate_verification: 'The responsible agent needs to link the valid result to the exact implementation it verified.',
  resolve_verification_conflict: 'Review and reconcile the conflicting verification results.',
  review_decision: 'Complete the evidence and reviews required by the design decision.',
  complete_lifecycle: 'Review the completion checks, then use the available task status transition.',
  resolve_native_gate: 'Resolve the outstanding task completion requirement.',
  inspect_lifecycle: 'Review the available status transitions and their requirements.',
  restore_evaluation: 'Restore the required information or access, then refresh the task.',
  request_authorized_decision: 'Ask an authorized person to review and decide.',
};

export function taskDeliveryActionText(kind: string, reasonCode: string): string {
  // Core uses this action for both missing implementation and repairing a
  // failed check. The latter must not imply that accepted implementation is missing.
  if (kind === 'implement_contribution' && reasonCode === 'verification_failed') {
    return taskDeliveryReasons.verification_failed.next;
  }
  return taskDeliveryActions[kind] || 'Review the action plan in the technical evidence.';
}
