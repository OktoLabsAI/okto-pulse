/**
 * KGSyncIndicator — small chip rendered on the KG canvas showing the live
 * sync state (spec f33eb9ca, card f5d197d4).
 *
 *   green  '✓ updated Xs ago'
 *   amber  '⟳ N new commits' + Apply button
 *   red    '⚠ disconnected'   (after 3 SSE failures or polling-fallback fails)
 */

import { useEffect, useState } from 'react';
import type { KgConnectionState } from '@/hooks/useKgLiveEvents';

interface Props {
  connectionState: KgConnectionState;
  unseenCommits: number;
  lastEventAt?: string | null;
  onApply: () => void;
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}

export function KGSyncIndicator({ connectionState, unseenCommits, lastEventAt, onApply }: Props) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  if (connectionState === 'disconnected') {
    return (
      <div
        data-testid="kg-sync-indicator"
        data-state="disconnected"
        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300"
        role="status"
      >
        <span aria-hidden>⚠</span> Disconnected
      </div>
    );
  }

  if (unseenCommits > 0) {
    return (
      <button
        type="button"
        data-testid="kg-sync-indicator"
        data-state="behind"
        onClick={onApply}
        className="inline-flex items-center gap-2 px-2.5 py-1 rounded-md text-xs bg-amber-100 text-amber-900 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-200"
      >
        <span aria-hidden>⟳</span>
        {unseenCommits} new commit{unseenCommits === 1 ? '' : 's'}
        <span className="ml-1 underline">Apply</span>
      </button>
    );
  }

  const ageSeconds = lastEventAt
    ? Math.max(0, (now - new Date(lastEventAt).getTime()) / 1000)
    : null;

  return (
    <div
      data-testid="kg-sync-indicator"
      data-state={connectionState === 'polling' ? 'polling' : 'live'}
      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300"
      role="status"
    >
      <span aria-hidden>✓</span>
      {connectionState === 'polling' ? 'Polling' : 'Live'}
      {ageSeconds !== null && <span className="opacity-75">· {formatAge(ageSeconds)} ago</span>}
    </div>
  );
}
