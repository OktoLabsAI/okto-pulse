/**
 * API client for the Discovery REST endpoints (/api/v1/discovery/).
 * Thin wrapper over fetch with typed responses from types/discovery.ts.
 */

import type {
  DiscoveryIntent,
  SavedSearch,
  SearchHistoryEntry,
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
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
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
