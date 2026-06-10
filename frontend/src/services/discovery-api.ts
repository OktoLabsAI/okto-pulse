/**
 * API client for the Discovery REST endpoints (/api/v1/discovery/).
 * Thin wrapper over fetch with typed responses from types/discovery.ts.
 */

import type {
  DiscoveryIntent,
  DiscoverySelectorOptionsResponse,
  SavedSearch,
  SearchHistoryEntry,
  SpecChildType,
} from '@/types/discovery';

/** Row in the normalized payload returned by POST /intents/:id/execute. */
export interface IntentExecutionRow {
  id: string;
  type: string;
  title: string;
  summary?: string | null;
  meta?: Record<string, unknown>;
}

/** Payload returned by the real-tool execution endpoint. */
export interface IntentExecutionResult {
  rows: IntentExecutionRow[];
  columns: string[];
  total: number;
  tool_binding: string;
  params_echo: Record<string, unknown>;
  execution: 'real_tool' | 'semantic_fallback';
  intent_id: string;
  intent_name: string;
  /** Extra fields some executors include (e.g. `summary` for blockers). */
  [extra: string]: unknown;
}

const BASE = '/api/v1/discovery';

async function dFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const detail = err?.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : typeof detail?.error === 'string'
          ? detail.error
          : typeof err?.message === 'string'
            ? err.message
            : `HTTP ${resp.status}`;
    throw new Error(message);
  }
  if (resp.status === 204 || resp.headers.get('Content-Length') === '0') {
    return undefined as T;
  }
  return resp.json();
}

export async function listIntents(): Promise<DiscoveryIntent[]> {
  return dFetch<DiscoveryIntent[]>('/intents');
}

export async function listSavedSearches(boardId: string): Promise<SavedSearch[]> {
  return dFetch<SavedSearch[]>(`/boards/${boardId}/saved-searches`);
}

export async function listSearchHistory(
  boardId: string,
): Promise<SearchHistoryEntry[]> {
  return dFetch<SearchHistoryEntry[]>(`/boards/${boardId}/search-history`);
}

export interface ListSelectorOptionsParams {
  selectorKind: 'spec' | 'spec_child' | 'card';
  specId?: string | null;
  childType?: SpecChildType | string | null;
  status?: string | null;
  q?: string | null;
  limit?: number;
  offset?: number;
  includeSuperseded?: boolean;
}

export async function listSelectorOptions(
  boardId: string,
  params: ListSelectorOptionsParams,
): Promise<DiscoverySelectorOptionsResponse> {
  const qs = new URLSearchParams();
  qs.set('selector_kind', params.selectorKind);
  if (params.specId) qs.set('spec_id', params.specId);
  if (params.childType) qs.set('child_type', params.childType);
  if (params.status) qs.set('status', params.status);
  if (params.q) qs.set('q', params.q);
  if (params.limit !== undefined) qs.set('limit', String(params.limit));
  if (params.offset !== undefined) qs.set('offset', String(params.offset));
  if (params.includeSuperseded !== undefined) {
    qs.set('include_superseded', String(params.includeSuperseded));
  }
  return dFetch<DiscoverySelectorOptionsResponse>(
    `/boards/${boardId}/selector-options?${qs.toString()}`,
  );
}

/**
 * Execute an intent against its real tool_binding. Closes the "semantic
 * fallback masking" gap from the v1 catalog (ideação a4f526df).
 */
export async function executeIntent(
  intentId: string,
  boardId: string,
  params: Record<string, unknown> = {},
): Promise<IntentExecutionResult> {
  return dFetch<IntentExecutionResult>(`/intents/${intentId}/execute`, {
    method: 'POST',
    body: JSON.stringify({ board_id: boardId, params }),
  });
}
