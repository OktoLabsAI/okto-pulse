import { describe, expect, it } from 'vitest';
import type {
  CodeTraceabilityEvidence,
  ContextualEvidenceCoverage,
  DeliveryContext,
  ObligationEvidenceMapping,

  SourceContextEvidenceItemV2,
  SourceContextSummaryV2,
} from '@/types';
import {


  codeEvidenceBaselinePresenceLabel,
  codeEvidenceContextOriginLabel,
  codeEvidenceSourceRoleLabel,
  contextualInvestigationOutcomeLabel,


  deliveryContextLabel,
  groupSourceContextEvidence,
  presentContextualEvidenceCoverage,

} from '../sourceContextPresentation';


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
    ]).toEqual([
      'Existing implementation',
      'Existing scaffold',
      'Existing constraint',
      'Reference pattern',
    ]);
    expect(codeEvidenceContextOriginLabel('authored')).toBe('Agent-authored context');
    expect(codeEvidenceBaselinePresenceLabel('committed_snapshot'))
      .toBe('Committed snapshot');
    expect(codeEvidenceBaselinePresenceLabel('preexisting_worktree'))
      .toBe('Pre-existing worktree');
  });

  it('TS-UI-EA67-02 joins by canonical IDs in stable order and never invents applicability', () => {
    const groups = groupSourceContextEvidence({
      evidence: [evidence('evidence-b'), evidence('evidence-a')],
      sourceContextItems: [
        sourceContextItem('evidence-c'),
        sourceContextItem('evidence-a', {
          source_role: 'existing_scaffold',
          evidence_applicable: false,
        }),
      ],

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
    expect(groups[2].sourceContextItem?.evidence_id).toBe('evidence-c');
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
      coverage({ total: 0, linked: 0, pending: 0, coverage_pct: null }),
      summary('brownfield', { evidence_applicable: null, investigation_outcome: null }),
    )).toMatchObject({ kind: 'not_calculated', percentage: null });
  });






});
