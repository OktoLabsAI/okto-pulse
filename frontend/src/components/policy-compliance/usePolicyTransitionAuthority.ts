import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useDashboardApi } from '@/services/api';
import type {
  AllowedTransition,
  AllowedTransitionEntityType,
} from '@/types';

import {
  isAllowedTransitionActionable,
  readPolicyTransitionRejection,
  requirePolicyTransitionEnvelope,
  type PolicyTransitionPreviewLoadState,
  type PolicyTransitionRejection,
} from './policyTransitionPreviewModel';

interface UsePolicyTransitionAuthorityOptions {
  boardId: string | null | undefined;
  entityType: AllowedTransitionEntityType;
  subjectId: string | null | undefined;
  currentStatus: string | null | undefined;
  enabled?: boolean;
  refreshKey?: number;
}

interface PolicyTransitionAuthority {
  preview: PolicyTransitionPreviewLoadState;
  transitions: AllowedTransition[];
  actionableTransitions: AllowedTransition[];
  rejection: PolicyTransitionRejection | null;
  refresh: () => Promise<void>;
  handleTransitionError: (
    error: unknown,
    toStatus: string,
  ) => Promise<boolean>;
  clearRejection: () => void;
}

const EMPTY_PREVIEW: PolicyTransitionPreviewLoadState = {
  status: 'ready',
  transitions: [],
  error: null,
};

/**
 * Loads the canonical lifecycle authority used by transition controls.
 *
 * The response is treated as untrusted until its complete B19 envelope is
 * validated. Requests are fenced by both generation and subject identity, so
 * stale responses can never enable an action for a different modal subject.
 */
export function usePolicyTransitionAuthority({
  boardId,
  entityType,
  subjectId,
  currentStatus,
  enabled = true,
  refreshKey = 0,
}: UsePolicyTransitionAuthorityOptions): PolicyTransitionAuthority {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;

  const requestGenerationRef = useRef(0);
  const subjectKey = [
    boardId ?? '',
    entityType,
    subjectId ?? '',
    currentStatus ?? '',
    enabled ? 'enabled' : 'disabled',
  ].join(':');
  const subjectKeyRef = useRef(subjectKey);
  subjectKeyRef.current = subjectKey;

  const [preview, setPreview] =
    useState<PolicyTransitionPreviewLoadState>(EMPTY_PREVIEW);
  const [rejection, setRejection] =
    useState<PolicyTransitionRejection | null>(null);

  const refresh = useCallback(async () => {
    const requestGeneration = ++requestGenerationRef.current;
    const requestedSubjectKey = subjectKey;
    const isCurrentRequest = () =>
      requestGenerationRef.current === requestGeneration
      && subjectKeyRef.current === requestedSubjectKey;

    if (
      !enabled
      || !boardId
      || !subjectId
      || !currentStatus
    ) {
      if (isCurrentRequest()) {
        setPreview(EMPTY_PREVIEW);
      }
      return;
    }

    setPreview({
      status: 'loading',
      transitions: [],
      error: null,
    });
    try {
      const response = await apiRef.current.getAllowedTransitions(boardId, {
        entity_type: entityType,
        entity_id: subjectId,
        current_status: currentStatus,
      });
      if (!isCurrentRequest()) return;
      const transitions = requirePolicyTransitionEnvelope(response, {
        boardId,
        entityType,
        subjectId,
        currentStatus,
      });
      setPreview({
        status: 'ready',
        transitions,
        error: null,
      });
    } catch (caught) {
      if (!isCurrentRequest()) return;
      setPreview({
        status: 'error',
        transitions: [],
        error: caught instanceof Error
          ? caught.message
          : 'The server transition contract could not be loaded.',
      });
    }
  }, [
    boardId,
    currentStatus,
    enabled,
    entityType,
    subjectId,
    subjectKey,
  ]);

  useEffect(() => {
    // A rejection belongs to one exact subject/status action context. Routine
    // authority refreshes for that same context must not erase the diagnostic;
    // switching subject, status, or availability always does.
    setRejection(null);
  }, [subjectKey]);

  useEffect(() => {
    void refresh();
  }, [refresh, refreshKey]);

  const {
    transitions,
    actionableTransitions,
  } = useMemo(() => {
    const authoritativeTransitions = preview.status === 'ready'
      ? preview.transitions
      : [];
    return {
      transitions: authoritativeTransitions,
      actionableTransitions:
        authoritativeTransitions.filter(isAllowedTransitionActionable),
    };
  }, [preview]);

  const handleTransitionError = useCallback(async (
    error: unknown,
    toStatus: string,
  ): Promise<boolean> => {
    if (!boardId || !subjectId || !currentStatus) return false;
    const parsed = readPolicyTransitionRejection(error, {
      boardId,
      entityType,
      subjectId,
      currentStatus,
      toStatus,
    });
    if (!parsed) return false;

    await refresh();
    if (subjectKeyRef.current === subjectKey) {
      setRejection(parsed);
    }
    return true;
  }, [
    boardId,
    currentStatus,
    entityType,
    refresh,
    subjectId,
    subjectKey,
  ]);

  const clearRejection = useCallback(() => {
    setRejection(null);
  }, []);

  return {
    preview,
    transitions,
    actionableTransitions,
    rejection,
    refresh,
    handleTransitionError,
    clearRejection,
  };
}
