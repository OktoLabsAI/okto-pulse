/**
 * Types mirroring okto_pulse.core.models.schemas DiscoveryIntentResponse
 * / SavedSearchResponse / SearchHistoryEntryResponse. Kept in sync
 * manually — when the backend Pydantic shape changes, update this file
 * in the same PR.
 */

export interface DiscoveryIntent {
  id: string;
  name: string;
  label: string;
  description: string | null;
  category: string;
  tool_binding: string;
  params_schema: Record<string, unknown> | null;
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
