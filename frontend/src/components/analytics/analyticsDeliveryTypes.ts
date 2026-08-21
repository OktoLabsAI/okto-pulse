export type AnalyticsAvailabilityState =
  | 'available'
  | 'partial'
  | 'empty'
  | 'not_applicable'
  | 'restricted'
  | 'unavailable'
  | 'inconsistent'
  | 'error'
  | string;

export interface SprintCommitmentProjection {
  state: 'available' | 'unavailable_legacy' | AnalyticsAvailabilityState;
  baseline_ref: string | null;
  activated_at?: string | null;
  original_member_count?: number | null;
  current_member_count?: number | null;
  added_count?: number | null;
  removed_count?: number | null;
  unavailable_reason: string | null;
}

export interface SprintForecastProjection {
  result_state: AnalyticsAvailabilityState;
  reason: string | null;
  as_of?: string | null;
  method?: string | null;
  source_authority?: {
    source_name?: string | null;
    source_version?: string | number | null;
    authoritative_timestamp_field?: string | null;
    authority_ref?: string | null;
  } | null;
  history?: {
    result_state: AnalyticsAvailabilityState;
    sample_size: number | null;
    window_from?: string | null;
    window_to?: string | null;
    reason?: string | null;
  } | null;
  completion?: {
    result_state: AnalyticsAvailabilityState;
    p50_at: string | null;
    p80_at: string | null;
    reason?: string | null;
  } | null;
  throughput?: {
    result_state: AnalyticsAvailabilityState;
    period_days?: number | null;
    p50_items?: number | null;
    p80_items?: number | null;
    reason?: string | null;
  } | null;
  risk?: {
    result_state: AnalyticsAvailabilityState;
    reason_codes: string[];
  } | null;
}

export interface SprintAnalyticsItem {
  sprint_id: string;
  title: string;
  status: string;
  spec_id: string;
  total_cards: number;
  done_cards: number;
  completion_rate: number;
  card_status_breakdown: Record<string, number>;
  evaluations_count: number;
  last_evaluation: {
    overall_score: number | null;
    recommendation: string | null;
    evaluator_name: string | null;
    created_at: string | null;
  } | null;
  task_validation_gate: {
    total_submitted: number;
    total_success: number;
    total_failed: number;
    rejection_reasons: Record<string, number>;
    first_pass_rate: number | null;
  };
  commitment: SprintCommitmentProjection;
  forecast?: SprintForecastProjection | null;
}

export interface SprintAnalyticsResponse {
  contract_version?: string;
  query_fingerprint?: string;
  as_of?: string;
  summary: {
    total_sprints: number;
    status_breakdown: Record<string, number>;
    avg_completion_rate: number | null;
    sprint_evaluation: {
      total_submitted: number;
      approve_rate: number | null;
      avg_overall_score: number | null;
    };
  };
  sprints: SprintAnalyticsItem[];
  forecast?: SprintForecastProjection | null;
}

export type AnalyticsScalar = string | number | boolean | null;

export interface AnalyticsFilterClause {
  field: string;
  operator: string;
  value: AnalyticsScalar | AnalyticsScalar[];
}

export interface AnalyticsSourceAuthority {
  authority: string;
  reference: string;
  timestamp_field: string;
}

export interface AnalyticsProjectionProvenance {
  observed_at: string;
  currentness: 'current' | 'partial' | 'stale' | 'unavailable';
  reason: string | null;
  sources: AnalyticsSourceAuthority[];
}

export interface AnalyticsPopulationScope {
  scope_ref: string;
  accessible_count: number;
  excluded_count: number;
}

export interface AnalyticsExclusionSummary {
  restricted_count: number;
  excluded_count: number;
  reasons: Array<{ reason: string; count: number }>;
}

export interface ForecastSourcePeriod {
  from: string;
  to: string;
}

export interface ForecastEstimate {
  point: number;
  lower_bound: number;
  upper_bound: number;
  confidence_level: number;
  horizon: string;
  assumptions: string[];
  sample_size: number;
  source_period: ForecastSourcePeriod;
  method_version: string;
}

export interface ForecastBacktest {
  state: 'available' | 'unavailable' | 'empty';
  error: number | null;
  calibration: number | null;
  method_version: string;
  sample_size: number;
  evaluation_window: ForecastSourcePeriod | null;
  reason: string | null;
}

interface DeliveryForecastBase {
  contract_version: string;
  dependency_versions: {
    analytics_foundation: string;
    delivery_phase_1: string;
  };
  query_fingerprint: string;
  filters: AnalyticsFilterClause[];
  as_of: string;
  board_id: string;
  result_state: 'available' | 'partial' | 'unavailable' | 'restricted' | 'empty' | 'error';
  provenance: AnalyticsProjectionProvenance;
  backtest: ForecastBacktest;
  population_scope: AnalyticsPopulationScope;
  exclusions: AnalyticsExclusionSummary;
}

export interface DeliveryForecastReadyResponse extends DeliveryForecastBase {
  readiness: {
    ready: true;
    state: 'ready';
    reason: null;
    remediation: null;
    actual_observations: number;
    required_observations: number;
    rule_version: string;
  };
  forecast: ForecastEstimate;
}

export interface DeliveryForecastNonReadyResponse extends DeliveryForecastBase {
  readiness: {
    ready: false;
    state: 'insufficient_history' | 'unavailable' | 'restricted' | 'empty';
    reason: string;
    remediation: string;
    actual_observations: number;
    required_observations: number;
    rule_version: string;
  };
  /** The closed v1 transport union omits forecast until readiness is true. */
  forecast?: never;
}

export type DeliveryForecastResponse =
  | DeliveryForecastReadyResponse
  | DeliveryForecastNonReadyResponse;
