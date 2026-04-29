/**
 * API client for the KG decay tick controllability endpoint
 * (spec 54399628 — Wave 2 NC f9732afc).
 *
 * Backs the "Run tick now" button on KGHealthView SchemaTickCard and the
 * "Save & run now" button on RuntimeSettingsPanel.
 */

export interface TickRunNowResponse {
  tick_id: string;
  status: string; // "running"
  scheduled_at: string; // ISO datetime
}

export interface TickRunNowError {
  error: string; // e.g. "tick_already_running"
  message: string;
}

const BASE = '/api/v1';

/**
 * POST /api/v1/kg/tick/run-now — trigger the KG decay tick manually.
 *
 * @param boardId Optional board scope. When undefined, the tick runs
 *   globally (all boards), preserving cron semantics.
 * @param forceFullRebuild When true, the backend zeroes out
 *   `last_recomputed_at` for nodes in scope BEFORE the tick — forces
 *   recompute even of fresh nodes (ignores staleness threshold).
 *
 * Throws when the backend returns 4xx/5xx; the error message is the
 * structured `detail.message` from FastAPI when present.
 */
export async function triggerKGTick(
  boardId?: string,
  forceFullRebuild = false,
): Promise<TickRunNowResponse> {
  const resp = await fetch(`${BASE}/kg/tick/run-now`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      board_id: boardId ?? null,
      force_full_rebuild: forceFullRebuild,
    }),
  });
  if (!resp.ok) {
    const err = await resp
      .json()
      .catch(() => ({ detail: { message: resp.statusText } }));
    // FastAPI HTTPException(detail={...}) shape
    const detail = err?.detail;
    if (detail && typeof detail === 'object') {
      const message = detail.message || JSON.stringify(detail);
      const wrapped = new Error(message) as Error & {
        status?: number;
        code?: string;
      };
      wrapped.status = resp.status;
      wrapped.code = detail.error;
      throw wrapped;
    }
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}
