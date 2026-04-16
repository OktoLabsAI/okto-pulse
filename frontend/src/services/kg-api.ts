/**
 * API client for Knowledge Graph REST endpoints (/api/kg/).
 * Thin wrapper over fetch with typed responses from types/knowledge-graph.ts.
 */

import type { KGNode, KGEdge, KGStats, KGSettings, AuditEntry, ContradictionPair } from '@/types/knowledge-graph';

const KG_BASE = '/api/v1/api/kg';

async function kgFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${KG_BASE}${path}`, {
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
  return resp.json();
}

// Nodes
export async function listNodes(boardId: string, params?: {
  type?: string;
  min_confidence?: number;
  limit?: number;
  cursor?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.type) qs.set('type', params.type);
  if (params?.min_confidence) qs.set('min_confidence', String(params.min_confidence));
  if (params?.limit) qs.set('limit', String(params.limit));
  if (params?.cursor) qs.set('cursor', params.cursor);
  return kgFetch<{ nodes: KGNode[]; next_cursor: string | null; total_hint: number }>(
    `/boards/${boardId}/nodes?${qs}`
  );
}

export async function getNodeDetail(boardId: string, nodeId: string) {
  return kgFetch<KGNode>(`/boards/${boardId}/nodes/${nodeId}`);
}

// Graph (for visualization)
export async function getSubgraph(boardId: string, params?: {
  center?: string;
  depth?: number;
  max_nodes?: number;
}) {
  const qs = new URLSearchParams();
  if (params?.center) qs.set('center', params.center);
  if (params?.depth) qs.set('depth', String(params.depth));
  if (params?.max_nodes) qs.set('max_nodes', String(params.max_nodes));
  return kgFetch<{ nodes: KGNode[]; edges: KGEdge[]; metadata: Record<string, unknown> }>(
    `/boards/${boardId}/graph?${qs}`
  );
}

// Stats
export async function getStats(boardId: string) {
  return kgFetch<KGStats>(`/boards/${boardId}/stats`);
}

// Audit
export async function listAudit(boardId: string, limit = 50) {
  return kgFetch<{ entries: AuditEntry[]; next_cursor: string | null }>(
    `/boards/${boardId}/audit?limit=${limit}`
  );
}

export async function undoSession(boardId: string, sessionId: string, force = false) {
  return kgFetch<Record<string, unknown>>(`/boards/${boardId}/audit/${sessionId}/undo`, {
    method: 'POST',
    body: JSON.stringify({ force }),
  });
}

// Global search
export async function globalSearch(query: string, limit = 20) {
  return kgFetch<{ results: Array<{ board_id: string; id: string; title: string; similarity: number }>; total: number }>(
    `/global/search?q=${encodeURIComponent(query)}&limit=${limit}`
  );
}

// Settings
export async function getKGSettings(boardId: string) {
  return kgFetch<KGSettings>(`/boards/${boardId}/settings`);
}

export async function updateKGSettings(boardId: string, settings: Partial<KGSettings>) {
  return kgFetch<{ success: boolean }>(`/boards/${boardId}/settings`, {
    method: 'PUT',
    body: JSON.stringify(settings),
  });
}

// Historical consolidation
export async function startHistorical(boardId: string) {
  return kgFetch<{ status: string; total_artifacts: number }>(`/boards/${boardId}/historical-consolidation/start`, {
    method: 'POST',
  });
}

export async function cancelHistorical(boardId: string) {
  return kgFetch<{ status: string }>(`/boards/${boardId}/historical-consolidation/cancel`, {
    method: 'POST',
  });
}

export async function getHistoricalProgress(boardId: string) {
  return kgFetch<{ enabled: boolean; status: string; total: number; progress: number }>(
    `/boards/${boardId}/historical-consolidation/progress`
  );
}

// Delete KG (right-to-erasure)
export async function deleteKG(boardId: string) {
  return kgFetch<void>(`/boards/${boardId}/kg`, { method: 'DELETE' });
}

// Schema
export async function getSchemaInfo(boardId?: string, includeInternal = false) {
  const qs = new URLSearchParams();
  if (boardId) qs.set('board_id', boardId);
  if (includeInternal) qs.set('include_internal', 'true');
  return kgFetch<Record<string, unknown>>(`/schema?${qs}`);
}
