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

export interface DeliveryMetric {
  state: AnalyticsAvailabilityState;
  value: number | null;
  numerator: number | null;
  denominator: number | null;
  sample_size: number;
  reason: string | null;
  unit: string | null;
}

export interface DeliveryContribution {
  subject_id: string | null;
  subject_label: string;
  visibility: 'self' | 'operator' | 'aggregate' | 'restricted';
  role: string;
  done_count: number | null;
  first_pass: DeliveryMetric;
  validation_success: DeliveryMetric;
  rework_introduced: number | null;
  rework_resolved: number | null;
  median_cycle_hours: DeliveryMetric;
  sample_size: number;
  period: { from: string; to: string };
}

export interface DeliveryIntelligenceResponse {
  contract_version: '2';
  foundation_version: string;
  query_fingerprint: string;
  filters: AnalyticsFilterClause[];
  as_of: string;
  board_id: string;
  result_state: 'available' | 'partial' | 'empty' | 'restricted' | 'unavailable' | 'error';
  provenance: AnalyticsProjectionProvenance;
  population_scope: AnalyticsPopulationScope;
  exclusions: AnalyticsExclusionSummary;
  minimum_sample_size: number;
  contributions: DeliveryContribution[];
  next_cursor: string | null;
}

export interface DeliveryIntelligenceFilters {
  role?: string;
  contributionView?: 'self' | 'aggregates' | 'self_and_aggregates' | 'operator';
  cursor?: string;
  limit?: number;
}
