/**
 * API client for the Discovery REST endpoints (/api/v1/discovery/).
 * Thin wrapper over fetch with typed responses from types/discovery.ts.
 */

import type {
  DiscoveryIntent,
  SavedSearch,
  SearchHistoryEntry,
} from '@/types/discovery';

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
