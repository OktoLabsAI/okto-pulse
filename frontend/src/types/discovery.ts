/**
 * Types mirroring okto_pulse.core.models.schemas DiscoveryIntentResponse
 * / SavedSearchResponse / SearchHistoryEntryResponse. Kept in sync
 * manually — when the backend Pydantic shape changes, update this file
 * in the same PR.
 */

export type DiscoveryParamType = 'text' | 'entity_selector' | 'spec_child_selector';

export type SpecChildType =
  | 'functional_requirement'
  | 'business_rule'
  | 'technical_requirement'
  | 'decision'
  | 'acceptance_criterion'
  | 'api_contract'
  | 'integration_requirement'
  | 'observability_requirement';

export interface DiscoveryParamSchema {
  type?: DiscoveryParamType | string;
  required?: boolean;
  label?: string;
  entity_type?: string;
  child_types?: SpecChildType[] | string[];
  depends_on?: string[];
  options_endpoint?: string;
}

export type DiscoveryParamsSchema = Record<string, DiscoveryParamSchema>;

export interface DiscoveryIntent {
  id: string;
  name: string;
  label: string;
  description: string | null;
  category: string;
  tool_binding: string;
  params_schema: DiscoveryParamsSchema | null;
  renderer: string;
  min_permission: string | null;
  active: boolean;
  is_seed: boolean;
  created_at: string;
  updated_at: string;
}

export interface SavedSearch {
  id: string;
  board_id: string;
  name: string;
  query: string | null;
  intent_id: string | null;
  filters_json: Record<string, unknown> | null;
  created_by: string | null;
  created_at: string;
}

export interface SearchHistoryEntry {
  id: string;
  board_id: string;
  user_id: string;
  query: string | null;
  intent_id: string | null;
  result_count: number;
  searched_at: string;
}

export interface DiscoverySelectorOption {
  id: string;
  label: string;
  entity_type: string;
  subtitle?: string;
  spec_id?: string;
  spec_title?: string;
  child_type?: SpecChildType | string;
  child_id?: string;
  child_index?: number;
  child_ref?: string;
  status?: string;
  version?: number | string;
  order?: number;
  refs?: Record<string, unknown>;
}

export interface DiscoverySelectorOptionsResponse {
  options: DiscoverySelectorOption[];
  source: string;
  cache_status: string;
  global_refs_used: boolean;
}

export interface DiscoverySpecChildSelectorValue {
  spec_id: string;
  child_type: SpecChildType | string;
  child_id: string;
  child_ref: string;
}
