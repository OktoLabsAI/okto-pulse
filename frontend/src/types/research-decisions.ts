/**
 * Standalone frontend contract for the Refinement Research Decision Ledger.
 *
 * This immutable ledger is intentionally separate from the historical
 * `Refinement.decisions: string[]` field. Legacy strings are display-only and
 * must never be sent to the append/supersede endpoints.
 */

export const RESEARCH_DECISION_PAGE_SIZES = [25, 50, 100] as const;

export type ResearchDecisionPageSize =
  (typeof RESEARCH_DECISION_PAGE_SIZES)[number];

export type ResearchDecisionStatus =
  | 'open'
  | 'investigating'
  | 'resolved'
  | 'deferred';

export type ResearchDecisionAnchorType =
  | 'functional_requirement'
  | 'acceptance_criterion'
  | 'technical_requirement'
  | 'qa';

export interface ResearchDecisionAnchor {
  anchor_type: ResearchDecisionAnchorType;
  anchor_ref: string;
}

export interface ResearchDecisionContent {
  unknown: string;
  status: ResearchDecisionStatus;
  anchor: ResearchDecisionAnchor;
  evidence_refs: string[];
  alternatives: string[];
  decision: string | null;
  rationale: string | null;
  confidence: number | null;
  evidence_absence_justification: string | null;
}

export interface ResearchDecisionEntry {
  id: string;
  ledger_id: string;
  board_id: string;
  refinement_id: string;
  refinement_version: number;
  predecessor_entry_id: string | null;
  content: ResearchDecisionContent;
  content_digest: string;
  created_by: string;
  created_at: string;
}

export interface ResearchDecisionPage {
  items: ResearchDecisionEntry[];
  offset: number;
  limit: ResearchDecisionPageSize;
  total_filtered: number;
  total_overall: number;
}

export interface ListResearchDecisionsOptions {
  offset?: number;
  limit?: ResearchDecisionPageSize;
  ledgerId?: string | null;
  status?: ResearchDecisionStatus | null;
  signal?: AbortSignal;
}

interface ResearchDecisionWriteRequestBase {
  idempotency_key: string;
  entry: ResearchDecisionContent;
}

export interface AppendResearchDecisionRequest
  extends ResearchDecisionWriteRequestBase {
  operation: 'append';
  expected_head_revision: 0;
  ledger_id?: never;
  supersedes_entry_id?: never;
}

export interface SupersedeResearchDecisionRequest
  extends ResearchDecisionWriteRequestBase {
  operation: 'supersede';
  expected_head_revision: number;
  ledger_id: string;
  supersedes_entry_id: string;
}

export type ResearchDecisionWriteRequest =
  | AppendResearchDecisionRequest
  | SupersedeResearchDecisionRequest;

export interface ResearchDecisionWriteResponse {
  entry_id: string;
  /** Backend compatibility name for the immutable ledger/thread id. */
  thread_id: string;
  sequence: number;
  head_revision: number;
  refinement_version: number;
  replayed: boolean;
  entry: ResearchDecisionEntry;
}

export interface ResearchDecisionHeadResponse {
  entry: ResearchDecisionEntry;
  head_revision: number;
}

export interface ResearchDecisionHeadResolution {
  entry: ResearchDecisionEntry;
  headRevision: number;
}

export interface ResearchDecisionErrorDetails {
  reason_code?: string;
  [key: string]: unknown;
}

/** Explicit alias used by integration props to prevent mixing legacy strings. */
export type LegacyRefinementDecision = string;
