export type CanonicalResultState =
  | 'available'
  | 'empty'
  | 'not_applicable'
  | 'restricted'
  | 'unavailable'
  | 'stale'
  | 'inconsistent'
  | string;

export interface CanonicalCoverageIdentity {
  spec_id: string;
  obligation_type?: string;
  obligation_id: string;
  edition: number;
  currentness?: string | null;
  spec_title?: string | null;
}

export interface CanonicalAnalyticsRecord {
  id?: string;
  target_id?: string;
  resolution_id?: string;
  execution_id?: string;
  overlap_id?: string;
  waiver_id?: string;
  receipt_id?: string | null;
  investigation_receipt_id?: string | null;
  state?: string | null;
  currentness?: string | null;
  lifecycle?: string | null;
  outcome?: string | null;
  age_seconds?: number | null;
  relative_path?: string | null;
  qualified_symbol?: string | null;
  selector?: string | null;
  reason?: string | null;
  reason_code?: string | null;
  [key: string]: unknown;
}

export interface CanonicalCodeEvidence {
  state: string;
  reason: string | null;
  currentness?: string | null;
  age_seconds?: number | null;
  receipt_id?: string | null;
  receipt_currentness?: string | null;
  targets: CanonicalAnalyticsRecord[];
  resolutions: CanonicalAnalyticsRecord[];
  executions: CanonicalAnalyticsRecord[];
  overlaps: CanonicalAnalyticsRecord[];
  waivers: CanonicalAnalyticsRecord[];
  [key: string]: unknown;
}

export interface CanonicalCoverageRow {
  identity: CanonicalCoverageIdentity;
  state: string;
  applicable?: boolean | null;
  covered: boolean | null;
  lifecycle?: string | null;
  outcome?: string | null;
  currentness?: string | null;
  age_seconds?: number | null;
  skip: {
    state: string;
    effective: boolean;
    authority_ref?: string | null;
    reason_code: string | null;
    currentness?: string | null;
    [key: string]: unknown;
  };
  authority_ref?: string | null;
  reason?: string | null;
  evidence?: Array<{
    evidence_id: string;
    evidence_type?: string | null;
    source_ref?: string | null;
    obligation?: string | null;
    relation_type?: string | null;
    evidence_content_sha256?: string | null;
    parent_card_id?: string | null;
    delivery_state?: string | null;
    lifecycle_status?: string | null;
    currentness?: string | null;
    currentness_reason?: string | null;
    authority_ref?: string | null;
    eligibility?: string | boolean | null;
    [key: string]: unknown;
  }>;
  [key: string]: unknown;
}

export interface CanonicalCoverageGroup {
  obligation_type: string;
  state: CanonicalResultState;
  applicable: number | null;
  covered: number | null;
  uncovered: number | null;
  skipped: number | null;
  value: number | null;
  n: number | null;
  reason: string | null;
  rows: CanonicalCoverageRow[];
}

export interface CanonicalCoverageResponse {
  contract_version?: string;
  query_fingerprint: string;
  as_of: string;
  totals: {
    state: CanonicalResultState;
    applicable: number | null;
    covered: number | null;
    uncovered: number | null;
    skipped: number | null;
    value: number | null;
    n: number | null;
    reason: string | null;
  };
  coverage: CanonicalCoverageGroup[];
  code_evidence?: CanonicalCodeEvidence | null;
  [key: string]: unknown;
}

export interface FlowHealthBlocker extends CanonicalAnalyticsRecord {
  code: string;
  message?: string | null;
  blocking?: boolean;
  authority_state?: string | null;
  authority_ref?: string | null;
  effective_skip?: boolean;
  remediation?: string | null;
  deeplink?: string | null;
  deep_link?: string | null;
}

export interface FlowHealthThreshold extends CanonicalAnalyticsRecord {
  threshold_hours?: number | null;
  threshold_seconds?: number | null;
  stale_after_hours?: number | null;
  stale_hours?: number | null;
  provenance?: string | null;
  policy_version?: number | null;
  authority_ref?: string | null;
  exceeded?: boolean | null;
  exceeded_by_seconds?: number | null;
}

export interface FlowHealthItem {
  subject: { type: string; id: string; title?: string | null; [key: string]: unknown };
  state: string;
  lifecycle?: string | null;
  outcome?: string | null;
  reason_codes: string[];
  current_episode: {
    state: string;
    age_seconds: number;
    entered_at?: string | null;
    entry_event_id?: string | null;
    authority_ref?: string | null;
    [key: string]: unknown;
  } | null;
  threshold?: FlowHealthThreshold | null;
  provenance?: CanonicalAnalyticsRecord | null;
  source_authority?: CanonicalAnalyticsRecord | null;
  blockers?: FlowHealthBlocker[];
  rework: CanonicalAnalyticsRecord[];
  rejected_recovery?: CanonicalAnalyticsRecord | CanonicalAnalyticsRecord[] | null;
  dependency_report?: CanonicalAnalyticsRecord | null;
  defect_report?: CanonicalAnalyticsRecord | null;
  execution_report?: CanonicalAnalyticsRecord | null;
  reports?: {
    rejected_recovery?: CanonicalAnalyticsRecord | CanonicalAnalyticsRecord[] | null;
    dependency?: CanonicalAnalyticsRecord | null;
    defect?: CanonicalAnalyticsRecord | null;
    execution?: CanonicalAnalyticsRecord | null;
    [key: string]: unknown;
  } | null;
  [key: string]: unknown;
}

export interface FlowHealthPolicy {
  version: number;
  general_stale_hours: number;
  rejected_stale_hours: number;
  overrides?: Record<string, unknown> | Array<{ state: string; stale_hours: number }>;
  source?: string | null;
  [key: string]: unknown;
}

export interface FlowHealthResponse {
  contract_version?: string;
  query_fingerprint: string;
  as_of: string;
  effective_policy: FlowHealthPolicy;
  summary: Record<string, number>;
  items: FlowHealthItem[];
  [key: string]: unknown;
}

export interface FlowHealthSettings {
  version: number;
  general_stale_hours: number;
  rejected_stale_hours: number;
  overrides: Record<string, number>;
}

export interface FlowHealthSettingsResponse {
  board_id: string;
  settings: FlowHealthSettings;
}

export interface FlowHealthSettingsUpdate {
  expected_version: number;
  general_stale_hours: number;
  rejected_stale_hours: number;
  overrides: Record<string, number>;
}

export type BoardKgAnalyticsState =
  | 'available'
  | 'partial'
  | 'restricted'
  | 'unavailable'
  | 'empty'
  | 'error';

export type BoardKgClassificationState =
  | 'healthy'
  | 'at_risk'
  | 'blocking'
  | 'unavailable'
  | 'restricted'
  | 'error';

export type BoardKgHealthState =
  | 'healthy'
  | 'at_risk'
  | 'backpressure'
  | 'recovery_needed'
  | 'quarantined';

export type BoardKgDomain =
  | 'active_queue'
  | 'technical_dlq'
  | 'canonical_debt'
  | 'policy_projection_debt'
  | 'cognitive_backlog';

export type BoardKgDomainSeverity = 'informational' | 'at_risk' | 'blocking';
export const BOARD_KG_COGNITIVE_STATUSES = [
  'pending',
  'in_progress',
  'consolidated',
  'skipped',
  'failed',
  'no_action',
] as const;
export type BoardKgCognitiveStatus = typeof BOARD_KG_COGNITIVE_STATUSES[number];
export type BoardKgProvenanceKind = 'deterministic' | 'cognitive' | 'fallback' | 'legacy';
export type BoardKgEffectivenessState = 'available' | 'empty' | 'unavailable' | 'restricted';

export interface BoardKgAnalyticsQueryOptions {
  cognitiveStatus?: readonly BoardKgCognitiveStatus[];
  artifactTypes?: readonly string[];
  cursor?: string | null;
  limit?: number;
}

export interface BoardKgHealthComponent {
  component: string;
  health_state: BoardKgHealthState;
  result_state: BoardKgAnalyticsState;
  classification_reason: string;
}

export interface BoardKgDomainAge {
  result_state: BoardKgAnalyticsState;
  sample_count: number;
  p50_hours: number | null;
  p95_hours: number | null;
  oldest_hours: number | null;
  reason: string | null;
}

export interface BoardKgDrillDown {
  allowed: boolean;
  target: string | null;
}

export interface BoardKgOperationalDomain {
  domain: BoardKgDomain;
  result_state: BoardKgAnalyticsState;
  count: number | null;
  severity: BoardKgDomainSeverity | null;
  age: BoardKgDomainAge;
  drill_down: BoardKgDrillDown;
  reason: string | null;
}

export interface BoardKgCognitiveInventory {
  result_state: BoardKgAnalyticsState;
  by_status: Partial<Record<BoardKgCognitiveStatus, number>>;
  total: number | null;
  overdue_revisits: number | null;
  age: BoardKgDomainAge;
  reason: string | null;
}

export interface BoardKgTiming {
  state: BoardKgEffectivenessState;
  sample_count: number;
  p50_hours: number | null;
  p95_hours: number | null;
  reason: string | null;
}

export interface BoardKgEffectiveness {
  state: BoardKgEffectivenessState;
  numerator: number | null;
  denominator: number | null;
  rate: number | null;
  candidate_count: number | null;
  persisted_count: number | null;
  conversion_rate: number | null;
  method_version: string;
  sample_period: { from: string; to: string };
  timing: BoardKgTiming;
  reason: string | null;
}

export interface BoardKgProvenanceMix {
  result_state: BoardKgAnalyticsState;
  total: number | null;
  by_kind: Partial<Record<BoardKgProvenanceKind, { count: number; rate: number | null }>>;
  reason: string | null;
}

export interface BoardKgDiagnostic {
  domain: string;
  severity: BoardKgDomainSeverity;
  reason: string;
  next_step: BoardKgDrillDown;
}

interface BoardKgAnalyticsResponseBase {
  contract_version: string;
  foundation_version: string;
  query_fingerprint: string;
  filters: Array<{
    field: string;
    operator: string;
    value: string | number | boolean | null | Array<string | number | boolean | null>;
  }>;
  as_of: string;
  board_id: string;
  result_state: BoardKgAnalyticsState;
  health: {
    state: BoardKgClassificationState;
    classification_reason: string;
    reason_codes: string[];
    availability: Record<BoardKgDomain, BoardKgAnalyticsState> & Record<string, BoardKgAnalyticsState>;
    components: BoardKgHealthComponent[];
  };
  population_scope: {
    scope_ref: string;
    accessible_count: number;
    excluded_count: number;
  };
  exclusions: {
    restricted_count: number;
    excluded_count: number;
    reasons: Array<{ reason: string; count: number }>;
  };
}

export interface BoardKgEffectivenessResponse extends BoardKgAnalyticsResponseBase {
  query: {
    window: { from: string; to: string };
    cognitive_status: BoardKgCognitiveStatus[];
    artifact_types: string[];
    cursor: string | null;
    limit: number;
  };
  provenance: {
    observed_at: string;
    currentness: 'current' | 'partial' | 'stale' | 'unavailable';
    reason: string | null;
    sources: Array<{ authority: string; reference: string; timestamp_field: string }>;
  };
  domains: BoardKgOperationalDomain[];
  cognitive_inventory: BoardKgCognitiveInventory;
  effectiveness: BoardKgEffectiveness;
  provenance_mix: BoardKgProvenanceMix;
  diagnostics: BoardKgDiagnostic[];
  redactions: string[];
  next_cursor: string | null;
}

/**
 * The Analytics UI consumes only the canonical v2 projection.  Keeping the
 * retired v1 debt_domains/cognitive_effectiveness shape in this union made a
 * partial response look valid and reintroduced the exact health/availability
 * ambiguity that v2 removes.
 */
export type BoardKgAnalyticsResponse = BoardKgEffectivenessResponse;
