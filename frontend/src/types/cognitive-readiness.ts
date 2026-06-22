/**
 * Types for the Cognitive Action Center (S3.3 / card 974f5146, spec 2731a346).
 *
 * Mirrors the core read-model DTO returned by
 * `GET /api/v1/kg/{board_id}/cognitive-readiness/items` and the MCP twin. The
 * UI NEVER recomputes precedence or enforcement — `readiness_effect`,
 * `precedence_explanation`, `blocking` and `would_block_done` are surfaced
 * verbatim from the backend (br_ee939fc7 / S3.1 carry-forward).
 */

// Per-row signal classification — the closed filter vocabulary.
export type ReadinessSignal =
  | 'cognitive_pending'
  | 'skipped'
  | 'revisit_required'
  | 'open_canonical_debt'
  | 'terminal_history'
  | 'dlq';

export type ReadinessSignalFilter = 'all' | ReadinessSignal;

export type ReadinessSignalSource = 'cognitive_item' | 'canonical_debt' | 'dlq';

// Bounded readiness effect from the central service (never recomputed here).
export type ReadinessEffect =
  | 'blocking_technical'
  | 'blocking_cognitive'
  | 'blocking_revisit_lapsed'
  | 'ready_skip'
  | 'ready_committed'
  | 'advisory'
  | 'ready';

export interface CognitiveReadinessItem {
  artifact_id: string;
  source_ref_original: string;
  aliases: string[];
  artifact_type: string;
  signal: ReadinessSignal;
  signal_source: ReadinessSignalSource;
  status: string | null;
  outcome_type: string | null;
  reason_code: string | null; // cognitive only
  error_cause: string | null; // technical only (technical_dlq / canonical_debt_open)
  revisit_at: string | null;
  readiness_effect: ReadinessEffect;
  blocking: boolean;
  precedence_explanation: Record<string, unknown>;
  would_block_done: boolean;
}

export interface CognitiveReadinessSummary {
  by_signal: Record<string, number>;
  technical_blocking_signals: number;
  cognitive_pending_signals: number;
  enforcement_active: boolean;
  total: number;
  limit: number;
  offset: number;
}

export interface CognitiveReadinessListResponse {
  board_id?: string;
  items: CognitiveReadinessItem[];
  summary: CognitiveReadinessSummary;
  precedence: string[];
}

export interface CognitiveReadinessMetrics {
  board_id: string;
  total: number;
  by_status: Record<string, number>;
  by_outcome_type: Record<string, number>;
  by_reason_code: Record<string, number>;
  by_artifact_type: Record<string, number>;
  by_readiness_effect: Record<string, number>;
  by_signal: Record<string, number>;
  by_signal_source: Record<string, number>;
  by_age_bucket: Record<string, number>;
  technical_blocking_signals: number;
  cognitive_pending_signals: number;
  expired_revisit_skips: number;
  open_canonical_debt: number;
  technical_dlq: number;
  terminal_history: number;
}

export interface CognitiveSkipResponse {
  item_id: string;
  status: string;
  outcome_type: string | null;
  reason_code: string | null;
  justification: string | null;
  evidence_refs: string[];
  actor: string;
  revisit_at: string | null;
  updated_at: string | null;
  classification: 'terminal' | 'revisit_required';
  readiness_effect: ReadinessEffect;
  blocking: boolean;
  would_block_done: boolean;
  precedence_explanation: Record<string, unknown>;
}

export interface CognitiveClearResponse {
  item_id: string;
  status: string;
  reason_code: string | null;
  revisit_at: string | null;
  actor: string;
  updated_at: string | null;
  readiness_effect: ReadinessEffect;
  blocking: boolean;
  would_block_done: boolean;
  precedence_explanation: Record<string, unknown>;
}

// Closed, cognitive-only, selectable reason_code registry (br_246210a3). The
// technical signals technical_dlq / canonical_debt_open / connectivity_guard
// are NEVER selectable here.
export const TERMINAL_REASON_CODES = [
  'no_reusable_learning',
  'duplicate_bug',
  'trivial_fix',
] as const;

export const REVISIT_REQUIRED_REASON_CODES = [
  'root_cause_unconfirmed',
  'evidence_insufficient',
  'path_b_pending',
  'external_context_missing',
] as const;

export const SELECTABLE_REASON_CODES = [
  ...TERMINAL_REASON_CODES,
  ...REVISIT_REQUIRED_REASON_CODES,
] as const;

export type SelectableReasonCode = (typeof SELECTABLE_REASON_CODES)[number];

export function isRevisitRequiredReason(code: string): boolean {
  return (REVISIT_REQUIRED_REASON_CODES as readonly string[]).includes(code);
}

// A row is a TECHNICAL blocker (resolve, never skip) when it carries an
// error_cause — DLQ or open canonical debt. The UI must NOT offer skip/no_action
// for these (the backend also rejects with 409).
export function isTechnicalBlocker(item: CognitiveReadinessItem): boolean {
  return item.signal === 'dlq' || item.signal === 'open_canonical_debt';
}
