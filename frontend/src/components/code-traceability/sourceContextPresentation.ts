import type {

  CodeEvidenceBaselinePresence,
  CodeEvidenceContextOrigin,
  CodeEvidenceSourceRole,
  CodeTraceabilityEvidence,
  ContextualEvidenceCoverage,
  ContextualInvestigationOutcomeV2,
  DeliveryContext,


  ObligationEvidenceMapping,

  SourceContextEvidenceItemV2,
  SourceContextSummaryV2,
} from '@/types';

export const DELIVERY_CONTEXT_LABELS = {
  brownfield: 'Brownfield',
  greenfield: 'Greenfield',
  hybrid: 'Hybrid',
} as const satisfies Readonly<Record<DeliveryContext, string>>;

export const CONTEXTUAL_INVESTIGATION_OUTCOME_LABELS = {
  evidence_applicable: 'Existing implementation found',
  no_relevant_existing_implementation: 'No relevant existing implementation',
  partial: 'Investigation partially available',
  unavailable: 'Investigation unavailable',
} as const satisfies Readonly<Record<ContextualInvestigationOutcomeV2, string>>;

export const CODE_EVIDENCE_SOURCE_ROLE_LABELS = {
  current_implementation: 'Existing implementation',
  existing_scaffold: 'Existing scaffold',
  existing_constraint: 'Existing constraint',
  reference_pattern: 'Reference pattern',

} as const satisfies Readonly<Record<CodeEvidenceSourceRole, string>>;

export const CODE_EVIDENCE_CONTEXT_ORIGIN_LABELS = {
  authored: 'Agent-authored context',


} as const satisfies Readonly<Record<CodeEvidenceContextOrigin, string>>;

export const CODE_EVIDENCE_BASELINE_PRESENCE_LABELS = {
  committed_snapshot: 'Committed snapshot',
  preexisting_worktree: 'Pre-existing worktree',
} as const satisfies Readonly<Record<CodeEvidenceBaselinePresence, string>>;



function compareText(left: string, right: string): number {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}



export function deliveryContextLabel(value: DeliveryContext): string {
  return DELIVERY_CONTEXT_LABELS[value];
}

export function contextualInvestigationOutcomeLabel(
  value: ContextualInvestigationOutcomeV2,
): string {
  return CONTEXTUAL_INVESTIGATION_OUTCOME_LABELS[value];
}

export function codeEvidenceSourceRoleLabel(value: CodeEvidenceSourceRole): string {
  return CODE_EVIDENCE_SOURCE_ROLE_LABELS[value];
}

export function codeEvidenceContextOriginLabel(
  value: CodeEvidenceContextOrigin,
): string {
  return CODE_EVIDENCE_CONTEXT_ORIGIN_LABELS[value];
}

export function codeEvidenceBaselinePresenceLabel(
  value: CodeEvidenceBaselinePresence,
): string {
  return CODE_EVIDENCE_BASELINE_PRESENCE_LABELS[value];
}

export interface SourceContextEvidenceGroup {
  evidenceId: string;
  evidence: CodeTraceabilityEvidence | null;
  sourceContextItem: SourceContextEvidenceItemV2 | null;

  obligationMappings: ObligationEvidenceMapping[];
  explicitlyApplicableMappings: ObligationEvidenceMapping[];
}

/**
 * Joins only server-projected identifiers and flags. It deliberately does not
 * derive a role, origin, applicability, or coverage value from Evidence text.
 */
export function groupSourceContextEvidence({
  evidence,
  sourceContextItems,

  obligationMappings,
}: {
  evidence: readonly CodeTraceabilityEvidence[];
  sourceContextItems: readonly SourceContextEvidenceItemV2[];

  obligationMappings: readonly ObligationEvidenceMapping[];
}): SourceContextEvidenceGroup[] {
  const evidenceById = new Map(evidence.map((item) => [item.id, item]));
  const contextById = new Map(sourceContextItems.map((item) => [item.evidence_id, item]));

  const mappingsById = new Map<string, ObligationEvidenceMapping[]>();

  for (const mapping of obligationMappings) {
    const entries = mappingsById.get(mapping.evidence_id) ?? [];
    entries.push(mapping);
    mappingsById.set(mapping.evidence_id, entries);
  }

  const evidenceIds = new Set<string>([
    ...evidenceById.keys(),
    ...contextById.keys(),
    ...mappingsById.keys(),
  ]);

  return [...evidenceIds]
    .sort(compareText)
    .map((evidenceId) => {
      const mappings = [...(mappingsById.get(evidenceId) ?? [])].sort(
        (left, right) => compareText(left.obligation_ref, right.obligation_ref)
          || compareText(left.relation_type, right.relation_type)
          || compareText(left.link_id, right.link_id),
      );
      return {
        evidenceId,
        evidence: evidenceById.get(evidenceId) ?? null,
        sourceContextItem: contextById.get(evidenceId) ?? null,

        obligationMappings: mappings,
        explicitlyApplicableMappings: mappings.filter(
          (mapping) => mapping.evidence_applicable === true,
        ),
      };
    });
}

export type ContextualCoveragePresentationKind =
  | 'projection_unavailable'
  | 'projection_incomplete'
  | 'not_applicable'
  | 'investigation_partial'
  | 'investigation_unavailable'
  | 'not_calculated'
  | 'covered'
  | 'pending';

export interface ContextualCoveragePresentation {
  kind: ContextualCoveragePresentationKind;
  label: string;
  description: string;
  percentage: number | null;
  determinate: boolean;
  countsAreLowerBounds: boolean;
}

/**
 * Maps the authoritative contextual aggregate to human copy. Counts and the
 * percentage are never recomputed from mappings.
 */
export function presentContextualEvidenceCoverage(
  coverage: ContextualEvidenceCoverage | null | undefined,
  sourceContext: SourceContextSummaryV2 | null | undefined,
): ContextualCoveragePresentation {
  if (!coverage) {
    return {
      kind: 'projection_unavailable',
      label: 'Coverage unavailable',
      description: 'The contextual coverage projection is not available.',
      percentage: null,
      determinate: false,
      countsAreLowerBounds: false,
    };
  }

  const base = {
    percentage: coverage.coverage_pct,
    determinate: coverage.coverage_pct !== null,
    countsAreLowerBounds: !coverage.projection_complete,
  };

  if (!coverage.projection_complete) {
    return {
      ...base,
      kind: 'projection_incomplete',
      label: 'Incomplete projection',
      description: 'Visible counts are lower bounds. Refresh or narrow the projection.',
      percentage: null,
      determinate: false,
    };
  }
  if (sourceContext?.investigation_outcome === 'partial') {
    return {
      ...base,
      kind: 'investigation_partial',
      label: 'Investigation incomplete',
      description: 'The source investigation returned only partial context.',
      percentage: null,
      determinate: false,
    };
  }
  if (sourceContext?.investigation_outcome === 'unavailable') {
    return {
      ...base,
      kind: 'investigation_unavailable',
      label: 'Investigation unavailable',
      description: 'The source investigation could not provide contextual evidence.',
      percentage: null,
      determinate: false,
    };
  }

  if (
    sourceContext?.evidence_applicable === false
    && sourceContext.investigation_outcome === 'no_relevant_existing_implementation'
    && coverage.total === 0
  ) {
    return {
      ...base,
      kind: 'not_applicable',
      label: 'Not applicable',
      description: 'No relevant existing implementation was found for this delivery context.',
      percentage: null,
      determinate: false,
    };
  }
  if (coverage.coverage_pct === null) {
    return {
      ...base,
      kind: 'not_calculated',
      label: 'Not calculated',
      description: 'The server did not publish a contextual coverage percentage.',
      determinate: false,
    };
  }
  if (coverage.pending === 0) {
    return {
      ...base,
      kind: 'covered',
      label: 'Covered',
      description: 'Every applicable evidence item is linked or dispositioned.',
    };
  }
  return {
    ...base,
    kind: 'pending',
    label: 'Pending',
    description: 'Applicable evidence still needs a link or disposition.',
  };
}
