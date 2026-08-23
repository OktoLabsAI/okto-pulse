import type {
  AuthoredCodeEvidenceSourceRole,
  CodeEvidenceBaselinePresence,
  CodeEvidenceContextOrigin,
  CodeEvidenceSourceRole,
  CodeTraceabilityEvidence,
  ContextualEvidenceCoverage,
  ContextualInvestigationOutcomeV2,
  DeliveryContext,
  LegacyEvidenceClassificationBatchRequest,
  LegacyEvidenceClassificationItemRequest,
  ObligationEvidenceMapping,
  SourceContextClassificationInputV2,
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
  uncategorized_legacy: 'Needs classification',
} as const satisfies Readonly<Record<CodeEvidenceSourceRole, string>>;

export const CODE_EVIDENCE_CONTEXT_ORIGIN_LABELS = {
  authored: 'Agent-authored context',
  human_legacy_classification: 'Classified legacy evidence',
  unclassified_legacy: 'Legacy evidence awaiting classification',
} as const satisfies Readonly<Record<CodeEvidenceContextOrigin, string>>;

export const CODE_EVIDENCE_BASELINE_PRESENCE_LABELS = {
  committed_snapshot: 'Committed snapshot',
  preexisting_worktree: 'Pre-existing worktree',
} as const satisfies Readonly<Record<CodeEvidenceBaselinePresence, string>>;

const AUTHORED_SOURCE_ROLES = new Set<CodeEvidenceSourceRole>([
  'current_implementation',
  'existing_scaffold',
  'existing_constraint',
  'reference_pattern',
]);

function compareText(left: string, right: string): number {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

function isAuthoredSourceRole(
  value: CodeEvidenceSourceRole,
): value is AuthoredCodeEvidenceSourceRole {
  return AUTHORED_SOURCE_ROLES.has(value);
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
  classificationInput: SourceContextClassificationInputV2 | null;
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
  classificationInputs,
  obligationMappings,
}: {
  evidence: readonly CodeTraceabilityEvidence[];
  sourceContextItems: readonly SourceContextEvidenceItemV2[];
  classificationInputs: readonly SourceContextClassificationInputV2[];
  obligationMappings: readonly ObligationEvidenceMapping[];
}): SourceContextEvidenceGroup[] {
  const evidenceById = new Map(evidence.map((item) => [item.id, item]));
  const contextById = new Map(sourceContextItems.map((item) => [item.evidence_id, item]));
  const inputById = new Map(classificationInputs.map((item) => [item.evidence_id, item]));
  const mappingsById = new Map<string, ObligationEvidenceMapping[]>();

  for (const mapping of obligationMappings) {
    const entries = mappingsById.get(mapping.evidence_id) ?? [];
    entries.push(mapping);
    mappingsById.set(mapping.evidence_id, entries);
  }

  const evidenceIds = new Set<string>([
    ...evidenceById.keys(),
    ...contextById.keys(),
    ...inputById.keys(),
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
        classificationInput: inputById.get(evidenceId) ?? null,
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
  | 'classification_required'
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
 * percentage are never recomputed from mappings or legacy coverage.
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
  if (coverage.unresolved_applicability_count > 0) {
    return {
      ...base,
      kind: 'classification_required',
      label: 'Needs classification',
      description: 'Legacy evidence must be classified before coverage can be calculated.',
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

export interface LegacyEvidenceClassificationDraft {
  readonly evidence_id: string;
  readonly expected_evidence_payload_sha256: string;
  readonly expected_classification_revision: number;
  source_role: AuthoredCodeEvidenceSourceRole | '';
  relevance_summary: string;
  scope_relation: string;
  source_origin: string;
  interpretation_limit: string;
  readonly baseline_presence: CodeEvidenceBaselinePresence;
  readonly baseline_workspace_state_id: string;
  provenance_note: string;
  readonly provenance_note_required: boolean;
}

export function createLegacyEvidenceClassificationDraft(
  input: SourceContextClassificationInputV2,
  effectiveItem?: SourceContextEvidenceItemV2 | null,
): LegacyEvidenceClassificationDraft {
  const matchingItem = effectiveItem?.evidence_id === input.evidence_id
    ? effectiveItem
    : null;
  const sourceRole = matchingItem && isAuthoredSourceRole(matchingItem.source_role)
    ? matchingItem.source_role
    : '';
  return {
    evidence_id: input.evidence_id,
    expected_evidence_payload_sha256: input.expected_evidence_payload_sha256,
    expected_classification_revision: input.expected_classification_revision,
    source_role: sourceRole,
    relevance_summary: matchingItem?.relevance_summary ?? '',
    scope_relation: matchingItem?.scope_relation ?? '',
    source_origin: matchingItem?.source_origin ?? '',
    interpretation_limit: matchingItem?.interpretation_limit ?? '',
    baseline_presence: input.baseline_provenance.presence,
    baseline_workspace_state_id: input.baseline_provenance.workspace_state_id,
    provenance_note: input.baseline_provenance.provenance_note ?? '',
    provenance_note_required: input.baseline_provenance.provenance_note_required,
  };
}

export type LegacyEvidenceClassificationDraftField =
  | 'items'
  | 'evidence_id'
  | 'expected_evidence_payload_sha256'
  | 'expected_classification_revision'
  | 'source_role'
  | 'relevance_summary'
  | 'scope_relation'
  | 'source_origin'
  | 'interpretation_limit'
  | 'baseline_workspace_state_id'
  | 'provenance_note'
  | 'justification';

export interface LegacyEvidenceClassificationDraftIssue {
  evidenceId: string | null;
  field: LegacyEvidenceClassificationDraftField;
  code: 'required' | 'invalid' | 'duplicate' | 'limit_exceeded';
  message: string;
}

function requiredTextIssue(
  draft: LegacyEvidenceClassificationDraft,
  field: 'relevance_summary' | 'scope_relation' | 'source_origin',
): LegacyEvidenceClassificationDraftIssue | null {
  if (draft[field].trim()) return null;
  return {
    evidenceId: draft.evidence_id,
    field,
    code: 'required',
    message: 'This field is required.',
  };
}

export function validateLegacyEvidenceClassificationDrafts(
  drafts: readonly LegacyEvidenceClassificationDraft[],
  justification: string,
): LegacyEvidenceClassificationDraftIssue[] {
  const issues: LegacyEvidenceClassificationDraftIssue[] = [];
  if (drafts.length === 0) {
    issues.push({
      evidenceId: null,
      field: 'items',
      code: 'required',
      message: 'Select at least one legacy evidence item.',
    });
  }
  if (drafts.length > 100) {
    issues.push({
      evidenceId: null,
      field: 'items',
      code: 'limit_exceeded',
      message: 'A classification batch can contain at most 100 items.',
    });
  }
  if (!justification.trim()) {
    issues.push({
      evidenceId: null,
      field: 'justification',
      code: 'required',
      message: 'A governance justification is required.',
    });
  }

  const seen = new Set<string>();
  for (const draft of drafts) {
    if (seen.has(draft.evidence_id)) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'evidence_id',
        code: 'duplicate',
        message: 'Each evidence item can appear only once in a batch.',
      });
    }
    seen.add(draft.evidence_id);
    if (!draft.evidence_id.trim()) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'evidence_id',
        code: 'required',
        message: 'A canonical evidence identifier is required.',
      });
    }
    if (!/^[a-f0-9]{64}$/iu.test(draft.expected_evidence_payload_sha256)) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'expected_evidence_payload_sha256',
        code: 'invalid',
        message: 'The evidence payload fence is invalid.',
      });
    }
    if (
      !Number.isInteger(draft.expected_classification_revision)
      || draft.expected_classification_revision < 0
    ) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'expected_classification_revision',
        code: 'invalid',
        message: 'The classification revision fence is invalid.',
      });
    }
    if (!draft.source_role || !isAuthoredSourceRole(draft.source_role)) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'source_role',
        code: 'required',
        message: 'Choose how this source may be interpreted.',
      });
    }
    for (const field of ['relevance_summary', 'scope_relation', 'source_origin'] as const) {
      const issue = requiredTextIssue(draft, field);
      if (issue) issues.push(issue);
    }
    if (
      (draft.source_role === 'existing_scaffold' || draft.source_role === 'reference_pattern')
      && !draft.interpretation_limit.trim()
    ) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'interpretation_limit',
        code: 'required',
        message: 'Explain the limit so this source is not mistaken for current behavior.',
      });
    }
    if (!draft.baseline_workspace_state_id.trim()) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'baseline_workspace_state_id',
        code: 'required',
        message: 'The canonical baseline workspace is required.',
      });
    }
    if (draft.provenance_note_required && !draft.provenance_note.trim()) {
      issues.push({
        evidenceId: draft.evidence_id,
        field: 'provenance_note',
        code: 'required',
        message: 'Describe why this source was already present in the frozen worktree.',
      });
    }
  }
  return issues;
}

export type LegacyEvidenceClassificationIntent = Omit<
  LegacyEvidenceClassificationBatchRequest,
  'idempotency_key'
>;

export class LegacyEvidenceClassificationDraftError extends Error {
  readonly issues: LegacyEvidenceClassificationDraftIssue[];

  constructor(issues: LegacyEvidenceClassificationDraftIssue[]) {
    super('legacy_evidence_classification_draft_invalid');
    this.name = 'LegacyEvidenceClassificationDraftError';
    this.issues = issues;
  }
}

function nullableTrimmed(value: string): string | null {
  const trimmed = value.trim();
  return trimmed || null;
}

export function buildLegacyEvidenceClassificationIntent(
  drafts: readonly LegacyEvidenceClassificationDraft[],
  justification: string,
): LegacyEvidenceClassificationIntent {
  const issues = validateLegacyEvidenceClassificationDrafts(drafts, justification);
  if (issues.length > 0) throw new LegacyEvidenceClassificationDraftError(issues);

  const items: LegacyEvidenceClassificationItemRequest[] = [...drafts]
    .sort((left, right) => compareText(left.evidence_id, right.evidence_id))
    .map((draft) => ({
      evidence_id: draft.evidence_id,
      expected_evidence_payload_sha256: draft.expected_evidence_payload_sha256,
      expected_classification_revision: draft.expected_classification_revision,
      source_role: draft.source_role as AuthoredCodeEvidenceSourceRole,
      relevance_summary: draft.relevance_summary.trim(),
      scope_relation: draft.scope_relation.trim(),
      source_origin: draft.source_origin.trim(),
      interpretation_limit: nullableTrimmed(draft.interpretation_limit),
      baseline_provenance: {
        presence: draft.baseline_presence,
        workspace_state_id: draft.baseline_workspace_state_id,
        provenance_note: nullableTrimmed(draft.provenance_note),
      },
    }));
  return { items, justification: justification.trim() };
}

function serializeClassificationIntent(intent: LegacyEvidenceClassificationIntent): string {
  const items = [...intent.items]
    .sort((left, right) => compareText(left.evidence_id, right.evidence_id))
    .map((item) => ({
      evidence_id: item.evidence_id,
      expected_evidence_payload_sha256: item.expected_evidence_payload_sha256,
      expected_classification_revision: item.expected_classification_revision,
      source_role: item.source_role,
      relevance_summary: item.relevance_summary,
      scope_relation: item.scope_relation,
      source_origin: item.source_origin,
      interpretation_limit: item.interpretation_limit,
      baseline_provenance: {
        presence: item.baseline_provenance.presence,
        workspace_state_id: item.baseline_provenance.workspace_state_id,
        provenance_note: item.baseline_provenance.provenance_note,
      },
    }));
  return JSON.stringify({ items, justification: intent.justification });
}

export interface LegacyClassificationIdempotencyKeyStore {
  keyFor(intent: LegacyEvidenceClassificationIntent): string;
  forget(intent: LegacyEvidenceClassificationIntent): void;
  clear(): void;
}

/** Keeps one key for byte-equivalent transport intent and rotates on change. */
export function createLegacyClassificationIdempotencyKeyStore(
  createKey: () => string,
): LegacyClassificationIdempotencyKeyStore {
  const keys = new Map<string, string>();
  return {
    keyFor(intent) {
      const serialized = serializeClassificationIntent(intent);
      const existing = keys.get(serialized);
      if (existing) return existing;
      const created = createKey().trim();
      if (!created || created.length > 512) {
        throw new Error('legacy_evidence_classification_idempotency_key_invalid');
      }
      keys.set(serialized, created);
      return created;
    },
    forget(intent) {
      keys.delete(serializeClassificationIntent(intent));
    },
    clear() {
      keys.clear();
    },
  };
}

export function attachLegacyClassificationIdempotencyKey(
  intent: LegacyEvidenceClassificationIntent,
  idempotencyKey: string,
): LegacyEvidenceClassificationBatchRequest {
  const normalizedKey = idempotencyKey.trim();
  if (!normalizedKey || normalizedKey.length > 512) {
    throw new Error('legacy_evidence_classification_idempotency_key_invalid');
  }
  return { ...intent, idempotency_key: normalizedKey };
}
