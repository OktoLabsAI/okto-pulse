import { useEffect, useRef, useState } from 'react';
import { useApiClient } from '@/contexts/ApiContext';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useDashboardStore } from '@/store/dashboard';
import type { WorkspaceSubjectRef } from '@/types';
import { WorkspaceRevisionWatch, type WorkspaceChange, type WorkspaceSyncState } from './workspaceRevisionWatch';

const watches = new WeakMap<object, Map<string, WorkspaceRevisionWatch>>();

export function useWorkspaceRevisions({ boardId, subjects, onChange, onAccessLost }: {
  boardId: string | undefined;
  subjects: WorkspaceSubjectRef[];
  onChange: (change: WorkspaceChange) => void;
  onAccessLost: () => void;
}) {
  const client = useApiClient();
  const api = useDashboardApi();
  const callbacks = useRef({ onChange, onAccessLost });
  callbacks.current = { onChange, onAccessLost };
  const [state, setState] = useState<WorkspaceSyncState>('connecting');
  const identity = JSON.stringify(subjects);
  useEffect(() => {
    if (!boardId || subjects.length === 0) return;
    let byBoard = watches.get(client);
    if (!byBoard) { byBoard = new Map(); watches.set(client, byBoard); }
    let watch = byBoard.get(boardId);
    if (!watch) {
      watch = new WorkspaceRevisionWatch(boardId, api.getWorkspaceRevisions, isCurrent => {
        // A subject-read denial does not imply loss of Board access. Confirm
        // through its native read before removing shell/navigation caches.
        void api.getBoard(boardId).catch(error => {
          if (isCurrent() && error instanceof AuthenticatedFetchError
            && [401, 403, 404].includes(error.status)) {
            useDashboardStore.getState().removeInaccessibleBoard(boardId);
          }
        });
      });
      byBoard.set(boardId, watch);
    }
    return watch.subscribe({
      subjects: JSON.parse(identity) as WorkspaceSubjectRef[],
      onChange: change => callbacks.current.onChange(change),
      onState: setState,
      onAccessLost: () => callbacks.current.onAccessLost(),
    });
  }, [client, api, boardId, identity]);
  return state;
}
