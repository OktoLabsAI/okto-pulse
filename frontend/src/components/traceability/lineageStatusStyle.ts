/** Stable status colors independent of entity type; labels remain authoritative. */
export const LINEAGE_STATUS_COLORS: Readonly<Record<string, string>> = {
  draft: '#94a3b8',
  not_started: '#a8a29e',
  started: '#38bdf8',
  in_progress: '#3b82f6',
  active: '#06b6d4',
  review: '#f59e0b',
  evaluating: '#d946ef',
  validation: '#a78bfa',
  validated: '#2dd4bf',
  approved: '#84cc16',
  done: '#22c55e',
  closed: '#14b8a6',
  cancelled: '#f87171',
  rejected: '#e11d48',
  on_hold: '#fb923c',
  blocked: '#dc2626',
  triage: '#facc15',
  ready: '#818cf8',
  converted: '#c084fc',
  automated: '#0ea5e9',
  passed: '#4ade80',
  failed: '#fb7185',
};

export function lineageStatusColor(status?: string | null): string {
  return LINEAGE_STATUS_COLORS[(status || '').trim().toLowerCase()] || '#64748b';
}
