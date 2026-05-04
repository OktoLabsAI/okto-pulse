/**
 * EmptyState — shown when a board has no KG data yet.
 * Hero + CTA to enable historical consolidation with progress tracking.
 */

import { useState, useEffect, useRef } from 'react';
import toast from 'react-hot-toast';
import * as kgApi from '@/services/kg-api';
import { KGHelpModal } from './KGHelpModal';

interface Props {
  boardId: string;
  onRefresh?: () => void;
}

export function EmptyState({ boardId, onRefresh }: Props) {
  const [loading, setLoading] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [progressInfo, setProgressInfo] = useState<kgApi.HistoricalProgress | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Check if there's already a consolidation in progress on mount
  useEffect(() => {
    if (!boardId) return;
    kgApi.getHistoricalProgress(boardId).then((p) => {
      if (p.enabled) {
        setProgressInfo(p);
        if (kgApi.isHistoricalProgressTerminal(p)) {
          onRefresh?.();
        } else if (kgApi.isHistoricalProgressActive(p)) {
          startPolling();
        }
      }
    }).catch(() => {});
    return () => stopPolling();
  }, [boardId]);

  const startPolling = () => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      try {
        const p = await kgApi.getHistoricalProgress(boardId);
        setProgressInfo(p);
        if (!kgApi.isHistoricalProgressActive(p)) {
          stopPolling();
          if (kgApi.isHistoricalProgressTerminal(p)) {
            if (p.status === 'completed_with_errors' || (p.failed ?? 0) > 0) {
              toast.error('Historical consolidation completed with errors');
            } else {
              toast.success('Historical consolidation complete!');
            }
            onRefresh?.();
          }
        }
      } catch {
        // keep polling
      }
    }, 3000);
  };

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const handleEnableHistorical = async () => {
    if (!boardId) {
      toast.error('Board ID is required');
      return;
    }
    setLoading(true);
    try {
      const result = await kgApi.startHistorical(boardId);
      if (result.status === 'already_in_progress') {
        toast('Historical consolidation is already in progress', { icon: 'ℹ️' });
      } else {
        toast.success(`Historical consolidation started: ${result.total_artifacts ?? 0} artifacts queued`);
      }
      // Fetch initial progress and start polling if enabled
      const p = await kgApi.getHistoricalProgress(boardId);
      if (p.enabled) {
        setProgressInfo(p);
        if (kgApi.isHistoricalProgressTerminal(p)) {
          onRefresh?.();
        } else if (kgApi.isHistoricalProgressActive(p)) {
          startPolling();
        }
      }
    } catch (err: any) {
      toast.error(err?.message || 'Failed to start historical consolidation');
    } finally {
      setLoading(false);
    }
  };

  const pct = progressInfo && progressInfo.total > 0
    ? Math.round((progressInfo.progress / progressInfo.total) * 100)
    : 0;

  const isProgressBlocked = kgApi.isHistoricalProgressActive(progressInfo);
  const isPaused = progressInfo?.status === 'paused';
  const isConsolidating = isProgressBlocked && !isPaused;
  const isComplete = kgApi.isHistoricalProgressTerminal(progressInfo);
  const hasErrors = progressInfo?.status === 'completed_with_errors' || (progressInfo?.failed ?? 0) > 0;
  const statusLabel = isPaused
    ? 'Consolidation paused'
    : isConsolidating
    ? 'Consolidation in progress...'
    : isComplete
      ? hasErrors
        ? 'Consolidation completed with errors'
        : 'Consolidation complete'
        : 'Consolidation queued';
  const progressCaption = isPaused
    ? `${pct}% complete; processing is paused.`
    : isConsolidating
    ? 'The MCP agent will process queued artifacts. This may take a few minutes.'
    : isComplete
      ? hasErrors
        ? `${pct}% complete; ${progressInfo?.failed ?? 0} artifact(s) need attention.`
        : `${pct}% complete. Refreshing graph...`
      : `${pct}% complete`;
  const actionLabel = loading
    ? 'Starting...'
    : isPaused
      ? 'Consolidation Paused'
    : isConsolidating
      ? 'Consolidation Running...'
      : isComplete
        ? 'Run Historical Consolidation Again'
        : 'Enable Historical Consolidation';

  return (
    <div className="flex flex-col items-center justify-center h-full text-center p-8" role="status">
      <div className="text-6xl mb-4">🕸️</div>
      <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
        Knowledge Graph is empty
      </h2>
      <p className="text-gray-500 dark:text-gray-400 mb-6 max-w-md">
        This board doesn't have any consolidated data in the knowledge graph yet.
        Enable historical consolidation to process existing specs and sprints,
        or wait for new consolidations via the code agent.
      </p>

      {/* Progress bar */}
      {progressInfo && progressInfo.enabled && (
        <div className="w-full max-w-md mb-6">
          <div className="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1.5">
            <span className="flex items-center gap-1.5">
              {isConsolidating && (
                <span className="inline-block w-2 h-2 rounded-full bg-violet-500 animate-pulse" />
              )}
              {statusLabel}
            </span>
            <span>{progressInfo.progress} / {progressInfo.total} artifacts</span>
          </div>
          <div className="w-full h-2.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-violet-500 rounded-full transition-all duration-500 ease-out"
              style={{ width: `${Math.max(pct, isConsolidating ? 3 : 0)}%` }}
            />
          </div>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
            {progressCaption}
          </p>
        </div>
      )}

      <div className="flex gap-3">
        <button
          onClick={handleEnableHistorical}
          disabled={loading || !boardId || isProgressBlocked}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {actionLabel}
        </button>
        <button
          onClick={() => setShowHelp(true)}
          className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 text-sm"
        >
          Learn How It Works
        </button>
      </div>

      {/* Help Modal */}
      {showHelp && <KGHelpModal onClose={() => setShowHelp(false)} />}
    </div>
  );
}
