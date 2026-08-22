import { describe, expect, it } from 'vitest';
import type {
  CodeTraceabilityEvidence,
  ContextualEvidenceCoverage,
  DeliveryContext,
  ObligationEvidenceMapping,
  SourceContextClassificationInputV2,
  SourceContextEvidenceItemV2,
  SourceContextSummaryV2,
} from '@/types';
import {
  attachLegacyClassificationIdempotencyKey,
  buildLegacyEvidenceClassificationIntent,
  codeEvidenceBaselinePresenceLabel,
  codeEvidenceContextOriginLabel,
  codeEvidenceSourceRoleLabel,
  contextualInvestigationOutcomeLabel,
  createLegacyClassificationIdempotencyKeyStore,
  createLegacyEvidenceClassificationDraft,
  deliveryContextLabel,
  groupSourceContextEvidence,
  presentContextualEvidenceCoverage,
  validateLegacyEvidenceClassificationDrafts,
} from '../sourceContextPresentation';

const SHA_A = 'a'.repeat(64);
const SHA_B = 'b'.repeat(64);

function evidence(id: string): CodeTraceabilityEvidence {
  return {
    id,
    investigation_receipt_id: `receipt-${id}`,
    source_ref: 'repo://main',
    parent_type: 'refinement',
    parent_id: 'refinement-1',
    parent_version: 3,
    evidence_type: 'structure',
    selector_kind: 'file',
    relative_path: `src/${id}.ts`,
    language: 'typescript',
    symbol_kind: null,
    qualified_symbol: null,
    attestation_state: 'agent_attested',
    lifecycle_status: 'active',
    supersedes_evidence_id: null,
  };
}

function sourceContextItem(
  evidenceId: string,
  overrides: Partial<SourceContextEvidenceItemV2> = {},
): SourceContextEvidenceItemV2 {
  return {
    evidence_id: evidenceId,
    source_role: 'current_implementation',
    relevance_summary: 'Relevant to the delivery scope.',
    scope_relation: 'Same bounded scope.',
    source_origin: 'Repository baseline.',
    interpretation_limit: null,
    baseline_provenance: {
      presence: 'committed_snapshot',
      workspace_state_id: 'workspace-1',
      provenance_note: null,
    },
    context_origin: 'authored',
    context_contract_version: 2,
    evidence_applicable: true,
    ...overrides,
  };
}

function classificationInput(
  evidenceId: string,
  overrides: Partial<SourceContextClassificationInputV2> = {},
): SourceContextClassificationInputV2 {
  return {
    evidence_id: evidenceId,
    expected_evidence_payload_sha256: SHA_A,
    expected_classification_revision: 0,
    baseline_provenance: {
      presence: 'committed_snapshot',
      workspace_state_id: 'workspace-1',
      provenance_note: null,
      provenance_note_required: false,
    },
    ...overrides,
  };
}

function mapping(
  linkId: string,
  evidenceId: string,
  applicability: boolean | null,
  obligationRef: string,
): ObligationEvidenceMapping {
  const [obligationType, obligationId] = obligationRef.split(':');
  return {
    link_id: linkId,
    evidence_id: evidenceId,
    obligation_type: obligationType,
    obligation_id: obligationId,
    obligation_ref: obligationRef,
    relation_type: 'supports',
    evidence_applicable: applicability,
    context_origin: applicability === null ? null : 'authored',
    source_role: applicability === true ? 'current_implementation' : 'existing_scaffold',
  };
}

function summary(
  deliveryContext: DeliveryContext,
  overrides: Partial<SourceContextSummaryV2> = {},
): SourceContextSummaryV2 {
  return {
    delivery_context: deliveryContext,
    delivery_context_provenance: {
      value: deliveryContext,
      source_refinement_id: 'refinement-1',
      source_refinement_version: 3,
    },
    investigation_outcome: 'evidence_applicable',
    role_counts: {
      current_implementation_count: 1,
      existing_scaffold_count: 0,
      existing_constraint_count: 0,
      reference_pattern_count: 0,
      uncategorized_legacy_count: 0,
    },
    classification_state: {
      classified_count: 1,
      uncategorized_legacy_count: 0,
    },
    evidence_applicable: true,
    interpretation_rule: 'Treat only current implementation as delivered behavior.',
    items_not_current_implementation_count: 0,
    technical_details_available: true,
    ...overrides,
  };
}

function coverage(
  overrides: Partial<ContextualEvidenceCoverage> = {},
): ContextualEvidenceCoverage {
  return {
    total: 1,
    linked: 1,
    dispositioned: 0,
    pending: 0,
    pending_ids: [],
    unresolved_applicability_count: 0,
    coverage_pct: 100,
    projection_complete: true,
    ...overrides,
  };
}

describe('Source Context presentation — UI spec edition 8 / card ea67', () => {
  it('TS-UI-EA67-01 gives every closed contextual value a concise human label', () => {
    expect([
      deliveryContextLabel('brownfield'),
      deliveryContextLabel('greenfield'),
      deliveryContextLabel('hybrid'),
    ]).toEqual([
      'Brownfield',
      'Greenfield',
      'Hybrid',
    ]);
    expect(contextualInvestigationOutcomeLabel('evidence_applicable'))
      .toBe('Existing implementation found');
    expect(contextualInvestigationOutcomeLabel('no_relevant_existing_implementation'))
      .toBe('No relevant existing implementation');
    expect(contextualInvestigationOutcomeLabel('partial'))
      .toBe('Investigation partially available');
    expect(contextualInvestigationOutcomeLabel('unavailable'))
      .toBe('Investigation unavailable');
    expect([
      codeEvidenceSourceRoleLabel('current_implementation'),
      codeEvidenceSourceRoleLabel('existing_scaffold'),
      codeEvidenceSourceRoleLabel('existing_constraint'),
      codeEvidenceSourceRoleLabel('reference_pattern'),
      codeEvidenceSourceRoleLabel('uncategorized_legacy'),
    ]).toEqual([
      'Existing implementation',
      'Existing scaffold',
      'Existing constraint',
      'Reference pattern',
      'Needs classification',
    ]);
    expect(codeEvidenceContextOriginLabel('authored')).toBe('Agent-authored context');
    expect(codeEvidenceContextOriginLabel('human_legacy_classification'))
      .toBe('Classified legacy evidence');
    expect(codeEvidenceContextOriginLabel('unclassified_legacy'))
      .toBe('Legacy evidence awaiting classification');
    expect(codeEvidenceBaselinePresenceLabel('committed_snapshot'))
      .toBe('Committed snapshot');
    expect(codeEvidenceBaselinePresenceLabel('preexisting_worktree'))
      .toBe('Pre-existing worktree');
  });

  it('TS-UI-EA67-02 joins by canonical IDs in stable order and never invents applicability', () => {
    const groups = groupSourceContextEvidence({
      evidence: [evidence('evidence-b'), evidence('evidence-a')],
      sourceContextItems: [
        sourceContextItem('evidence-a', {
          source_role: 'existing_scaffold',
          evidence_applicable: false,
        }),
      ],
      classificationInputs: [classificationInput('evidence-c')],
      obligationMappings: [
        mapping('link-3', 'evidence-a', false, 'technical_requirement:tr-2'),
        mapping('link-2', 'evidence-a', true, 'acceptance_criterion:ac-1'),
        mapping('link-1', 'evidence-b', null, 'functional_requirement:fr-1'),
      ],
    });

    expect(groups.map((group) => group.evidenceId)).toEqual([
      'evidence-a',
      'evidence-b',
      'evidence-c',
    ]);
    expect(groups[0].obligationMappings.map((item) => item.obligation_ref)).toEqual([
      'acceptance_criterion:ac-1',
      'technical_requirement:tr-2',
    ]);
    expect(groups[0].explicitlyApplicableMappings.map((item) => item.link_id))
      .toEqual(['link-2']);
    expect(groups[1].sourceContextItem).toBeNull();
    expect(groups[1].explicitlyApplicableMappings).toEqual([]);
    expect(groups[2].evidence).toBeNull();
    expect(groups[2].classificationInput?.expected_classification_revision).toBe(0);
  });

  it('TS-UI-EA67-03 presents authoritative Brownfield and Hybrid percentages without recomputing them', () => {
    const brownfield = presentContextualEvidenceCoverage(
      coverage({ total: 4, linked: 1, dispositioned: 1, pending: 2, coverage_pct: 37.5 }),
      summary('brownfield'),
    );
    const hybrid = presentContextualEvidenceCoverage(
      coverage({ total: 2, linked: 2, pending: 0, coverage_pct: 91.25 }),
      summary('hybrid'),
    );

    expect(brownfield).toMatchObject({
      kind: 'pending',
      percentage: 37.5,
      determinate: true,
    });
    expect(hybrid).toMatchObject({
      kind: 'covered',
      percentage: 91.25,
      determinate: true,
    });
  });

  it('TS-UI-EA67-04 presents Greenfield absence without synthetic Evidence, waiver, skip, or 100%', () => {
    const greenfield = presentContextualEvidenceCoverage(
      coverage({ total: 0, linked: 0, pending: 0, coverage_pct: null }),
      summary('greenfield', {
        investigation_outcome: 'no_relevant_existing_implementation',
        evidence_applicable: false,
        role_counts: {
          current_implementation_count: 0,
          existing_scaffold_count: 0,
          existing_constraint_count: 0,
          reference_pattern_count: 0,
          uncategorized_legacy_count: 0,
        },
        classification_state: {
          classified_count: 0,
          uncategorized_legacy_count: 0,
        },
      }),
    );

    expect(greenfield).toMatchObject({
      kind: 'not_applicable',
      label: 'Not applicable',
      percentage: null,
      determinate: false,
    });

    expect(presentContextualEvidenceCoverage(
      coverage({ total: 1, linked: 0, pending: 1, coverage_pct: null }),
      summary('greenfield', {
        investigation_outcome: 'no_relevant_existing_implementation',
        evidence_applicable: false,
      }),
    )).toMatchObject({ kind: 'not_calculated', percentage: null });
  });

  it('TS-UI-EA67-05 keeps partial and unavailable investigations indeterminate', () => {
    const partial = presentContextualEvidenceCoverage(
      coverage({ coverage_pct: null }),
      summary('hybrid', { investigation_outcome: 'partial' }),
    );
    const unavailable = presentContextualEvidenceCoverage(
      coverage({ total: 0, linked: 0, pending: 0, coverage_pct: null }),
      summary('brownfield', { investigation_outcome: 'unavailable' }),
    );

    expect(partial).toMatchObject({
      kind: 'investigation_partial',
      percentage: null,
      determinate: false,
    });
    expect(unavailable).toMatchObject({
      kind: 'investigation_unavailable',
      percentage: null,
      determinate: false,
    });
  });

  it('TS-UI-EA67-06 distinguishes missing, incomplete, unresolved, and authoritative null projections', () => {
    expect(presentContextualEvidenceCoverage(undefined, summary('brownfield')))
      .toMatchObject({ kind: 'projection_unavailable', percentage: null });
    expect(presentContextualEvidenceCoverage(
      coverage({ projection_complete: false, coverage_pct: 50 }),
      summary('brownfield'),
    )).toMatchObject({
      kind: 'projection_incomplete',
      percentage: null,
      determinate: false,
      countsAreLowerBounds: true,
    });
    expect(presentContextualEvidenceCoverage(
      coverage({ unresolved_applicability_count: 2, coverage_pct: null }),
      summary('hybrid'),
    )).toMatchObject({ kind: 'classification_required', percentage: null });
    expect(presentContextualEvidenceCoverage(
      coverage({ total: 0, linked: 0, pending: 0, coverage_pct: null }),
      summary('brownfield', { evidence_applicable: null, investigation_outcome: null }),
    )).toMatchObject({ kind: 'not_calculated', percentage: null });
  });

  it('TS-UI-EA67-07 validates conditional interpretation and server-required worktree provenance', () => {
    const draft = createLegacyEvidenceClassificationDraft(
      classificationInput('legacy-1', {
        baseline_provenance: {
          presence: 'preexisting_worktree',
          workspace_state_id: 'workspace-dirty',
          provenance_note: null,
          provenance_note_required: true,
        },
      }),
      sourceContextItem('legacy-1', {
        source_role: 'uncategorized_legacy',
        relevance_summary: null,
        scope_relation: null,
        source_origin: null,
        interpretation_limit: null,
        baseline_provenance: null,
        context_origin: 'unclassified_legacy',
        context_contract_version: null,
        evidence_applicable: null,
      }),
    );

    let issues = validateLegacyEvidenceClassificationDrafts([draft], 'Human review.');
    expect(issues.map((issue) => issue.field)).toEqual([
      'source_role',
      'relevance_summary',
      'scope_relation',
      'source_origin',
      'provenance_note',
    ]);

    Object.assign(draft, {
      source_role: 'existing_scaffold',
      relevance_summary: 'A starting structure exists.',
      scope_relation: 'Same repository and bounded area.',
      source_origin: 'Frozen pre-delivery worktree.',
      provenance_note: 'The scaffold predated this delivery request.',
    });
    issues = validateLegacyEvidenceClassificationDrafts([draft], 'Human review.');
    expect(issues.map((issue) => issue.field)).toEqual(['interpretation_limit']);

    draft.interpretation_limit = 'Structure only; behavior is not implemented.';
    expect(validateLegacyEvidenceClassificationDrafts([draft], 'Human review.')).toEqual([]);
  });

  it('TS-UI-EA67-08 preserves canonical fences and exact prior context when reclassifying legacy Evidence', () => {
    const input = classificationInput('legacy-1', {
      expected_evidence_payload_sha256: SHA_B,
      expected_classification_revision: 4,
      baseline_provenance: {
        presence: 'committed_snapshot',
        workspace_state_id: 'workspace-4',
        provenance_note: null,
        provenance_note_required: false,
      },
    });
    const effective = sourceContextItem('legacy-1', {
      source_role: 'existing_constraint',
      relevance_summary: 'The schema constrains the new behavior.',
      scope_relation: 'Shared persistence boundary.',
      source_origin: 'Committed schema baseline.',
      context_origin: 'human_legacy_classification',
      classification_revision: 4,
    });
    const draft = createLegacyEvidenceClassificationDraft(input, effective);
    const intent = buildLegacyEvidenceClassificationIntent(
      [draft],
      '  Correct the legacy classification.  ',
    );

    expect(draft).toMatchObject({
      expected_evidence_payload_sha256: SHA_B,
      expected_classification_revision: 4,
      source_role: 'existing_constraint',
    });
    expect(intent).toEqual({
      items: [{
        evidence_id: 'legacy-1',
        expected_evidence_payload_sha256: SHA_B,
        expected_classification_revision: 4,
        source_role: 'existing_constraint',
        relevance_summary: 'The schema constrains the new behavior.',
        scope_relation: 'Shared persistence boundary.',
        source_origin: 'Committed schema baseline.',
        interpretation_limit: null,
        baseline_provenance: {
          presence: 'committed_snapshot',
          workspace_state_id: 'workspace-4',
          provenance_note: null,
        },
      }],
      justification: 'Correct the legacy classification.',
    });
  });

  it('TS-UI-EA67-09 keeps idempotency stable for an exact intent and rotates when intent changes', () => {
    let sequence = 0;
    const keys = createLegacyClassificationIdempotencyKeyStore(
      () => `classification-key-${++sequence}`,
    );
    const draft = createLegacyEvidenceClassificationDraft(
      classificationInput('legacy-1'),
      sourceContextItem('legacy-1', { source_role: 'existing_constraint' }),
    );
    const intent = buildLegacyEvidenceClassificationIntent([draft], 'Reviewed by the owner.');
    const exactRetry = buildLegacyEvidenceClassificationIntent([draft], 'Reviewed by the owner.');
    const changedIntent = buildLegacyEvidenceClassificationIntent(
      [{ ...draft, relevance_summary: 'A corrected contextual summary.' }],
      'Reviewed by the owner.',
    );

    expect(keys.keyFor(intent)).toBe('classification-key-1');
    expect(keys.keyFor(exactRetry)).toBe('classification-key-1');
    expect(keys.keyFor(changedIntent)).toBe('classification-key-2');
    expect(attachLegacyClassificationIdempotencyKey(intent, keys.keyFor(intent)))
      .toMatchObject({ idempotency_key: 'classification-key-1' });

    keys.forget(intent);
    expect(keys.keyFor(intent)).toBe('classification-key-3');
  });
});
