/**
 * Board settings API client (NC-9 Wave 2 frontend).
 *
 * Reads and writes per-board settings (skip_test_evidence_global flag and
 * other future board-scoped knobs). Backed by `PATCH /api/v1/boards/:id`
 * which accepts BoardUpdate.settings (BoardSettings schema).
 */

import type { Board, BoardSettings } from '@/types';

const BASE = '/api/v1/boards';

export async function getBoardSettings(
  boardId: string,
): Promise<BoardSettings | null> {
  const resp = await fetch(`${BASE}/${boardId}`, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  const board: Board = await resp.json();
  return board.settings ?? null;
}

export async function patchBoardSettings(
  boardId: string,
  patch: Partial<BoardSettings>,
): Promise<BoardSettings | null> {
  const resp = await fetch(`${BASE}/${boardId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings: patch }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  const board: Board = await resp.json();
  return board.settings ?? null;
}
