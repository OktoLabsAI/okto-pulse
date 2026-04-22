/**
 * API client for the /me/permissions endpoint — returns the authenticated
 * user's effective permission flags for a given board.
 *
 * Used by usePermissions() to gate UI elements. The backend still enforces
 * authorization via 403 — this endpoint exists so the frontend can mirror
 * the same intent and avoid noisy clicks that would be rejected.
 */

export interface PermissionsResponse {
  board_id: string;
  preset_name: string | null;
  flags: Record<string, unknown>;
}

const BASE = '/api/v1';

export async function getMyPermissions(
  boardId: string,
): Promise<PermissionsResponse> {
  const qs = new URLSearchParams({ board_id: boardId });
  const resp = await fetch(`${BASE}/me/permissions?${qs.toString()}`, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.ok) {
    const err = await resp
      .json()
      .catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  return resp.json();
}
