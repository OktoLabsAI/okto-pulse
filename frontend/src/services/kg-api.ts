/**
 * API client for Knowledge Graph REST endpoints (/api/kg/).
 * Thin wrapper over fetch with typed responses from types/knowledge-graph.ts.
 */

import type { KGNode, KGEdge, KGStats, KGSettings, AuditEntry } from '@/types/knowledge-graph';

const KG_BASE = '/api/v1/kg';

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
  if (resp.status === 204 || resp.headers.get('Content-Length') === '0') {
    return undefined as T;
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
export interface SubgraphResponse {
  nodes: KGNode[];
  edges: KGEdge[];
  metadata: Record<string, unknown>;
  next_cursor: string | null;
}

export async function getSubgraph(boardId: string, params?: {
  center?: string;
  depth?: number;
  limit?: number;
  cursor?: string;
}) {
  const qs = new URLSearchParams();
  if (params?.center) qs.set('center', params.center);
  if (params?.depth) qs.set('depth', String(params.depth));
  if (params?.limit) qs.set('limit', String(params.limit));
  if (params?.cursor) qs.set('cursor', params.cursor);
  return kgFetch<SubgraphResponse>(`/boards/${boardId}/graph?${qs}`);
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

// Similar nodes
export async function findSimilar(boardId: string, topic: string, topK = 10) {
  return kgFetch<{
    results: Array<{
      id: string;
      title: string;
      source_artifact_ref?: string;
      similarity: number;
      combined_score: number;
    }>;
    total: number;
  }>(`/boards/${boardId}/similar?topic=${encodeURIComponent(topic)}&top_k=${topK}`);
}

// Supersedence chain
export async function getSupersedenceChain(boardId: string, decisionId: string) {
  return kgFetch<{
    chain: Array<{
      id: string;
      title: string;
      created_at?: string;
      superseded_by?: string;
      superseded_at?: string;
    }>;
    depth: number;
    current_active: string;
  }>(`/boards/${boardId}/supersedence/${decisionId}`);
}

// Contradictions
export async function findContradictions(boardId: string, nodeId?: string, limit = 50) {
  const qs = new URLSearchParams();
  if (nodeId) qs.set('node_id', nodeId);
  qs.set('limit', String(limit));
  return kgFetch<{
    contradictions: Array<{
      id_a: string;
      title_a: string;
      id_b: string;
      title_b: string;
      confidence: number;
    }>;
    total: number;
  }>(`/boards/${boardId}/contradictions?${qs}`);
}

// Global search
export async function globalSearch(query: string, limit = 20, minSimilarity = 0.3) {
  return kgFetch<{
    results: Array<{
      board_id: string;
      id: string;
      digest_id?: string;
      title: string;
      summary?: string;
      node_type?: string;
      similarity: number;
    }>;
    total: number;
  }>(
    `/global/search?q=${encodeURIComponent(query)}&limit=${limit}&min_similarity=${minSimilarity}`
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
  return kgFetch<{ status: string; total_artifacts?: number; board_id?: string }>(`/boards/${boardId}/historical-consolidation/start`, {
    method: 'POST',
  });
}

export async function cancelHistorical(boardId: string) {
  return kgFetch<{ status: string }>(`/boards/${boardId}/historical-consolidation/cancel`, {
    method: 'POST',
  });
}

export async function getHistoricalProgress(boardId: string) {
  return kgFetch<{
    enabled: boolean;
    status: string;
    total: number;
    progress: number;
    pending?: number;
    claimed?: number;
    paused?: number;
    failed?: number;
  }>(
    `/boards/${boardId}/historical-consolidation/progress`
  );
}

// Pending queue
export async function listPending(boardId: string) {
  return kgFetch<{
    entries: Array<{
      id: string;
      board_id: string;
      artifact_id: string;
      artifact_type: string;
      priority: string;
      source: string;
      status: string;
      triggered_at: string | null;
      claimed_by_session_id: string | null;
    }>;
    count: number;
  }>(`/boards/${boardId}/pending`);
}

// Pending queue — hierarchical tree (spec f33eb9ca)
export interface PendingTreeNode {
  id: string;
  type: 'ideation' | 'refinement' | 'spec' | 'sprint' | 'card';
  title: string;
  status: string;
  queue_entry_id?: string | null;
  retry_count?: number;
  age_seconds?: number;
  layer?: string;
  last_error?: string | null;
  children: PendingTreeNode[];
}

export interface PendingTreeLevels {
  ideations: { pending: number; in_progress: number; done: number; failed: number };
  refinements: PendingTreeLevels['ideations'];
  specs: PendingTreeLevels['ideations'];
  sprints: PendingTreeLevels['ideations'];
  cards: PendingTreeLevels['ideations'];
}

export async function getPendingTree(boardId: string, depth = 5) {
  return kgFetch<{
    board_id: string;
    depth: number;
    total_pending: number;
    levels: PendingTreeLevels;
    tree: PendingTreeNode[];
  }>(`/boards/${boardId}/pending/tree?depth=${depth}`);
}

export async function retryPending(boardId: string, queueEntryId: string, recursive = false) {
  return kgFetch<{
    board_id: string;
    queue_entry_id: string;
    recursive: boolean;
    reopened_count: number;
    reopened_ids: string[];
  }>(
    `/boards/${boardId}/pending/${queueEntryId}/retry?recursive=${recursive}`,
    { method: 'POST' },
  );
}

// Delete KG (right-to-erasure)
export async function deleteKG(boardId: string) {
  return kgFetch<void>(`/boards/${boardId}/kg`, { method: 'DELETE' });
}

// v0.3.0 R3 — manual relevance boost (+0.3, clamped at 1.5)
export interface BoostNodeResponse {
  node_id: string;
  node_type: string;
  score_before: number;
  score_after: number;
  boosted_at: string;
  boosted_by: string;
}

export async function boostNode(boardId: string, nodeId: string) {
  return kgFetch<BoostNodeResponse>(
    `/boards/${boardId}/nodes/${nodeId}/boost`,
    { method: 'POST' }
  );
}

// Schema
export async function getSchemaInfo(boardId?: string, includeInternal = false) {
  const qs = new URLSearchParams();
  if (boardId) qs.set('board_id', boardId);
  if (includeInternal) qs.set('include_internal', 'true');
  return kgFetch<Record<string, unknown>>(`/schema?${qs}`);
}
