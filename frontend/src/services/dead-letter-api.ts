/**
 * API client for the Dead Letter Inspector endpoint
 * (spec ed17b1fe — Wave 2 NC 1ede3471).
 *
 * GET /api/v1/kg/queue/dead-letter — list dead-lettered consolidation
 * rows for a board with pagination. Backs the DeadLetterInspectorModal
 * triggered from the RuntimeSettingsPanel "Open dead-letter inspector"
 * button.
 */

export interface DeadLetterErrorEntry {
  attempt: number;
  occurred_at: string;
  error_type: string;
  message: string;
  traceback: string | null;
}

export interface DeadLetterRow {
  id: string;
  board_id: string;
  artifact_type: string;
  artifact_id: string;
  original_queue_id: string | null;
  attempts: number;
  errors: DeadLetterErrorEntry[];
  dead_lettered_at: string | null;
}

export interface DeadLetterListResponse {
  rows: DeadLetterRow[];
  total: number;
  limit: number;
  offset: number;
}

const BASE = '/api/v1';

export async function getDeadLetterRows(
  boardId: string,
  limit = 50,
  offset = 0,
  signal?: AbortSignal,
): Promise<DeadLetterListResponse> {
  const params = new URLSearchParams({
    board_id: boardId,
    limit: String(limit),
    offset: String(offset),
  });
  const resp = await fetch(`${BASE}/kg/queue/dead-letter?${params}`, {
    headers: { 'Content-Type': 'application/json' },
    signal,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}
