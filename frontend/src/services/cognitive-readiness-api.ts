/**
 * API client for the Cognitive Action Center (S3.3 / card 974f5146).
 *
 * Consumes the core read-model + central write-path:
 *   GET  /api/v1/kg/{board_id}/cognitive-readiness/items
 *   GET  /api/v1/kg/{board_id}/cognitive-readiness/metrics
 *   POST /api/v1/kg/{board_id}/cognitive-readiness/skip
 *   POST /api/v1/kg/{board_id}/cognitive-readiness/clear
 *
 * The UI never recomputes precedence/enforcement — it renders what the backend
 * returns. skip/clear surface the CANONICAL error code + HTTP status so the UI
 * can show a 409 technical blocker without masking it (never offering skip for
 * DLQ / open canonical debt).
 */

import type {
  CognitiveClearResponse,
  CognitiveReadinessListResponse,
  CognitiveReadinessMetrics,
  CognitiveSkipResponse,
  ReadinessSignalFilter,
} from '@/types/cognitive-readiness';

const BASE = '/api/v1';

/** Typed error carrying the canonical backend code + HTTP status. */
export class ReadinessActionError extends Error {
  code: string;
  status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = 'ReadinessActionError';
    this.code = code;
    this.status = status;
  }
}

export interface ListReadinessOptions {
  signal?: ReadinessSignalFilter;
  artifactId?: string;
  sourceRef?: string;
  reasonCode?: string;
  status?: string;
  search?: string;
  limit?: number;
  offset?: number;
  kgGenerationId?: string;
}

export async function getReadinessItems(
  boardId: string,
  options: ListReadinessOptions = {},
  signal?: AbortSignal,
): Promise<CognitiveReadinessListResponse> {
  const params = new URLSearchParams();
  if (options.signal) params.set('signal', options.signal);
  if (options.artifactId) params.set('artifact_id', options.artifactId);
  if (options.sourceRef) params.set('source_ref', options.sourceRef);
  if (options.reasonCode) params.set('reason_code', options.reasonCode);
  if (options.status) params.set('status', options.status);
  if (options.search) params.set('search', options.search);
  params.set('limit', String(options.limit ?? 50));
  params.set('offset', String(options.offset ?? 0));
  if (options.kgGenerationId) params.set('kg_generation_id', options.kgGenerationId);

  const resp = await fetch(
    `${BASE}/kg/${encodeURIComponent(boardId)}/cognitive-readiness/items?${params}`,
    { headers: { 'Content-Type': 'application/json' }, signal },
  );
  if (!resp.ok) {
    throw await _toError(resp);
  }
  return resp.json();
}

export async function getReadinessMetrics(
  boardId: string,
  signal?: AbortSignal,
): Promise<CognitiveReadinessMetrics> {
  const resp = await fetch(
    `${BASE}/kg/${encodeURIComponent(boardId)}/cognitive-readiness/metrics`,
    { headers: { 'Content-Type': 'application/json' }, signal },
  );
  if (!resp.ok) {
    throw await _toError(resp);
  }
  return resp.json();
}

export interface RecordSkipPayload {
  sourceRef: string;
  reasonCode: string;
  justification?: string;
  evidenceRefs?: string[];
  revisitAt?: string;
}

export async function recordCognitiveSkip(
  boardId: string,
  payload: RecordSkipPayload,
): Promise<CognitiveSkipResponse> {
  const resp = await fetch(
    `${BASE}/kg/${encodeURIComponent(boardId)}/cognitive-readiness/skip`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_ref: payload.sourceRef,
        reason_code: payload.reasonCode,
        justification: payload.justification ?? null,
        evidence_refs: payload.evidenceRefs ?? null,
        revisit_at: payload.revisitAt ?? null,
      }),
    },
  );
  if (!resp.ok) {
    throw await _toError(resp);
  }
  return resp.json();
}

export async function clearCognitiveSkip(
  boardId: string,
  sourceRef: string,
): Promise<CognitiveClearResponse> {
  const resp = await fetch(
    `${BASE}/kg/${encodeURIComponent(boardId)}/cognitive-readiness/clear`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_ref: sourceRef }),
    },
  );
  if (!resp.ok) {
    throw await _toError(resp);
  }
  return resp.json();
}

/** Parse a FastAPI error body `{detail: {error, message, status_code}}` into a
 * typed ReadinessActionError preserving the canonical code + HTTP status. */
async function _toError(resp: Response): Promise<ReadinessActionError> {
  const body = await resp.json().catch(() => ({}) as Record<string, unknown>);
  const detail = (body as { detail?: unknown }).detail;
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>;
    return new ReadinessActionError(
      (d.error as string) || 'error',
      (d.message as string) || `HTTP ${resp.status}`,
      resp.status,
    );
  }
  const msg = typeof detail === 'string' ? detail : `HTTP ${resp.status}`;
  return new ReadinessActionError('error', msg, resp.status);
}
