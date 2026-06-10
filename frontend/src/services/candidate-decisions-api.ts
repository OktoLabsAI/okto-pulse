/**
 * Candidate decision REST client (KG-03A.4 + KG-03A.5).
 *
 * Two surfaces only:
 *
 *   GET  /api/v1/kg/cognitive-pending/candidate-decisions
 *   POST /api/v1/kg/cognitive-pending/candidate-decisions/{id}/command
 *
 * The GET is read-only and bounded; the POST validates the action via
 * the server-side bounded enum, enforces cross-board guard, unsafe
 * payload guard, and propagates outcome to the originating cognitive
 * pending item.
 */

const BASE = '/api/v1';

export type CandidateDecisionStatus =
  | 'proposed'
  | 'promoted'
  | 'linked'
  | 'dismissed'
  | 'no_action_required';

export interface CandidateDecisionItem {
  candidate_id: string;
  board_id: string;
  source_ref: string;
  source_generation_id: string;
  consolidation_session_id: string;
  title: string;
  rationale: string;
  evidence_refs: string[];
  status: CandidateDecisionStatus;
  created_by_agent_id: string;
  created_at: string;
  updated_at: string;
  formal_decision_ref: string | null;
  dismissed_reason_code: string | null;
  audit_ref: string | null;
}

export interface CandidateDecisionCounts {
  proposed: number;
  promoted: number;
  linked: number;
  dismissed: number;
  no_action_required: number;
  total: number;
}

export interface ListCandidateDecisionsResponse {
  board_id: string;
  readonly: true;
  counts: CandidateDecisionCounts;
  items: CandidateDecisionItem[];
}

export interface ListCandidateDecisionsOptions {
  status?: CandidateDecisionStatus | null;
  sourceRef?: string | null;
  limit?: number;
  offset?: number;
}

export type CandidateDecisionCommandAction =
  | 'promote_to_spec_decision'
  | 'link_existing_decision'
  | 'dismiss'
  | 'no_action_required';

export interface CandidateDecisionCommandRequest {
  board_id: string;
  action: CandidateDecisionCommandAction;
  // promote_to_spec_decision
  spec_id?: string;
  title?: string;
  rationale?: string;
  context?: string;
  alternatives_considered?: string[];
  supersedes_decision_id?: string;
  linked_requirements?: number[];
  notes?: string;
  // link_existing_decision
  formal_decision_id?: string;
  // dismiss / no_action_required
  reason_code?: string;
}

export interface CandidateDecisionCommandResponse {
  candidate_id: string;
  board_id: string;
  action: string;
  status: CandidateDecisionStatus;
  formal_decision_ref: string | null;
  formal_decision: Record<string, unknown> | null;
  dismissed_reason_code: string | null;
  audit_ref: string;
  updated_at: string;
}

async function handleResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const detail = err.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message ||
          detail?.code ||
          err.message ||
          `HTTP ${resp.status}`;
    const error = new Error(message) as Error & {
      code?: string;
      status?: number;
    };
    if (detail && typeof detail === 'object' && 'code' in detail) {
      error.code = String((detail as { code: unknown }).code);
    }
    error.status = resp.status;
    throw error;
  }
  return resp.json();
}

export async function listCandidateDecisions(
  boardId: string,
  options: ListCandidateDecisionsOptions = {},
  signal?: AbortSignal,
): Promise<ListCandidateDecisionsResponse> {
  const params = new URLSearchParams();
  params.set('board_id', boardId);
  if (options.status) params.set('status', options.status);
  if (options.sourceRef) params.set('source_ref', options.sourceRef);
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  if (options.offset !== undefined)
    params.set('offset', String(options.offset));
  const resp = await fetch(
    `${BASE}/kg/cognitive-pending/candidate-decisions?${params.toString()}`,
    { headers: { 'Content-Type': 'application/json' }, signal },
  );
  return handleResponse(resp);
}

export async function submitCandidateDecisionCommand(
  candidateId: string,
  body: CandidateDecisionCommandRequest,
  signal?: AbortSignal,
): Promise<CandidateDecisionCommandResponse> {
  const resp = await fetch(
    `${BASE}/kg/cognitive-pending/candidate-decisions/${encodeURIComponent(
      candidateId,
    )}/command`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    },
  );
  return handleResponse(resp);
}
