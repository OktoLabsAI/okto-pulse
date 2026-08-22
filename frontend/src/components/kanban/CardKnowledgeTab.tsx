/**
 * CardKnowledgeTab - read-only Knowledge Base snapshots for a card/task.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  RefreshCw,
  Shield,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { v4 as uuidv4 } from 'uuid';
import { useDashboardApi } from '@/services/api';
import type {
  Card,
  EffectiveResourceItem,
  KnowledgeAssignmentTechnicalProjection,
  KnowledgeTechnicalReadResponse,
  KnowledgeWorkspaceItem,
} from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { KnowledgeWorkspace } from '@/components/resources/KnowledgeWorkspace';
import {
  KnowledgePropagationSelector,
} from '@/components/shared/KnowledgePropagationSelector';
import {
  effectiveKnowledgeCandidate,
  mergeKnowledgePropagationCandidates,
  physicalKnowledgeCandidate,
  type KnowledgePropagationCandidate,
} from '@/components/shared/knowledgePropagationCandidates';
import {
  EMPTY_KNOWLEDGE_PROPAGATION_CHOICE,
  isKnowledgePropagationChoiceValid,
  type KnowledgePropagationChoice,
} from '@/components/shared/knowledgePropagationChoice';

interface CardKnowledgeTabProps {
  card: Card;
  specKnowledgeBases: {
    id: string;
    title: string;
    description?: string | null;
    content: string;
    mime_type?: string;
    root_source_kb_id?: string | null;
    governance?: Record<string, unknown>;
  }[];
  onUpdate: () => Promise<void>;
  onBusyChange?: (busy: boolean) => void;
  readOnly?: boolean;
}

const ORIGIN_CLASS_LABELS: Record<string, string> = {
  v2: 'v2',
  legacy_all: 'legacy all',
  selected_legacy: 'selected legacy',
  legacy_unresolved: 'legacy unresolved',
};

function assignmentTitle(
  assignment: KnowledgeAssignmentTechnicalProjection,
  candidates: KnowledgePropagationCandidate[],
): string {
  return candidates.find((item) => item.id === assignment.root_knowledge_id)?.title
    || assignment.root_knowledge_id;
}

function workspaceKnowledgeCandidate(
  item: KnowledgeWorkspaceItem,
): KnowledgePropagationCandidate {
  return {
    id: item.root_id,
    title: item.title || item.root_id,
    stale: item.stale,
    origin_class: item.provenance.origin_class,
  };
}

function mutationErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return 'Knowledge propagation operation failed';
}

function isKnowledgeConflict(error: unknown): boolean {
  if (error instanceof AuthenticatedFetchError) {
    return error.status === 409;
  }
  if (
    typeof error === 'object'
    && error !== null
    && 'status' in error
    && error.status === 409
  ) {
    return true;
  }
  return error instanceof Error && /revision|conflict/i.test(error.message);
}

export function CardKnowledgeTab({
  card,
  specKnowledgeBases,
  onUpdate,
  onBusyChange,
  readOnly = false,
}: CardKnowledgeTabProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  const [sourceEffectiveCandidates, setSourceEffectiveCandidates] = useState<
    KnowledgePropagationCandidate[]
  >([]);
  const [technicalRead, setTechnicalRead] = useState<KnowledgeTechnicalReadResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadGeneration, setReloadGeneration] = useState(0);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [refreshingRootId, setRefreshingRootId] = useState<string | null>(null);
  const [choice, setChoice] = useState<KnowledgePropagationChoice>(
    EMPTY_KNOWLEDGE_PROPAGATION_CHOICE,
  );
  const [mutationIdempotencyKey, setMutationIdempotencyKey] = useState<string>(
    () => uuidv4(),
  );
  const onBusyChangeRef = useRef(onBusyChange);
  const reportedBusyRef = useRef(false);
  const refreshIntentRef = useRef<{
    fingerprint: string;
    idempotencyKey: string;
  } | null>(null);
  const candidates = useMemo<KnowledgePropagationCandidate[]>(() => {
    const physical = specKnowledgeBases.map(physicalKnowledgeCandidate);
    const effective = sourceEffectiveCandidates;
    const assignments = (technicalRead?.assignments || []).map((assignment) => {
      const existing = [...physical, ...effective].find(
        (item) => item.id === assignment.root_knowledge_id,
      );
      return {
        id: assignment.root_knowledge_id,
        title: existing?.title || assignment.root_knowledge_id,
        description: existing?.description,
        stale: assignment.stale,
        origin_class: assignment.origin_class,
      };
    });
    return mergeKnowledgePropagationCandidates(
      physical,
      effective,
      assignments,
    );
  }, [sourceEffectiveCandidates, specKnowledgeBases, technicalRead]);

  const busy = mutationBusy || refreshingRootId !== null;

  useEffect(() => {
    onBusyChangeRef.current = onBusyChange;
  }, [onBusyChange]);

  useEffect(() => {
    if (reportedBusyRef.current === busy) return;
    reportedBusyRef.current = busy;
    onBusyChangeRef.current?.(busy);
  }, [busy]);

  useEffect(
    () => () => {
      if (reportedBusyRef.current) onBusyChangeRef.current?.(false);
    },
    [],
  );

  useEffect(() => {
    apiRef.current = api;
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);

    Promise.allSettled([
      Promise.resolve().then(() =>
        apiRef.current.getCardKnowledgeAssignments(card.id),
      ),
      Promise.resolve().then(async () => {
        if (!card.spec_id) return [] as KnowledgePropagationCandidate[];
        const collected: KnowledgePropagationCandidate[] = [];
        const consumedCursors = new Set<string>();
        let cursor: string | null = null;
        do {
          const response = await apiRef.current.getEffectiveResources(
            card.board_id,
            'spec',
            card.spec_id,
            {
              profile: 'summary',
              limit: 25,
              ...(cursor ? { cursor } : {}),
            },
          );
          const pageCandidates = Array.isArray(response.items)
            ? response.items
              .filter((item) => item.resource_type === 'knowledge_base')
              .map(workspaceKnowledgeCandidate)
            : (response.resources?.knowledge_base || [])
              .map((item: EffectiveResourceItem) => effectiveKnowledgeCandidate(item))
              .filter((item): item is KnowledgePropagationCandidate => item !== null);
          collected.push(...pageCandidates);
          cursor = response.next_cursor || null;
          if (cursor) {
            if (consumedCursors.has(cursor)) {
              throw new Error('Source spec Knowledge inventory returned a repeated cursor.');
            }
            consumedCursors.add(cursor);
          }
        } while (cursor);
        return mergeKnowledgePropagationCandidates(collected);
      }),
    ]).then(([technicalResult, sourceResult]) => {
      if (cancelled) return;
      const errors: string[] = [];
      if (technicalResult.status === 'fulfilled') {
        setTechnicalRead(technicalResult.value);
      } else {
        setTechnicalRead(null);
        errors.push('governed assignments');
      }
      if (sourceResult.status === 'fulfilled') {
        setSourceEffectiveCandidates(sourceResult.value);
      } else {
        setSourceEffectiveCandidates([]);
        errors.push('source spec Knowledge inventory');
      }
      if (errors.length > 0) {
        setLoadError(`Failed to load ${errors.join(' and ')}.`);
      }
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, [card.board_id, card.id, card.spec_id, reloadGeneration]);

  const updateChoice = (next: KnowledgePropagationChoice) => {
    const changed = JSON.stringify(next) !== JSON.stringify(choice);
    setChoice(next);
    if (changed) setMutationIdempotencyKey(uuidv4());
  };

  const reload = () => setReloadGeneration((current) => current + 1);

  const resetMutation = () => {
    setChoice(EMPTY_KNOWLEDGE_PROPAGATION_CHOICE);
    setMutationIdempotencyKey(uuidv4());
  };

  const handleSaveDecision = async () => {
    if (
      readOnly
      ||
      !technicalRead
      || choice.action === 'omitted'
      || !isKnowledgePropagationChoiceValid(choice)
    ) {
      return;
    }
    setMutationBusy(true);
    try {
      if (choice.action === 'drop') {
        await apiRef.current.dropCardKnowledgeAssignments(card.id, {
          contract_version: 2,
          knowledge_ids: choice.knowledgeIds,
          justification: choice.justification.trim(),
          idempotency_key: mutationIdempotencyKey,
          expected_revision: technicalRead.revision,
        });
      } else {
        await apiRef.current.replaceCardKnowledgeAssignments(card.id, {
          contract_version: 2,
          knowledge_ids: choice.knowledgeIds,
          mode: choice.action,
          justification: choice.justification.trim(),
          idempotency_key: mutationIdempotencyKey,
          expected_revision: technicalRead.revision,
          linkage: [],
        });
      }
      toast.success(
        choice.action === 'drop'
          ? 'Knowledge drop decision saved'
          : 'Knowledge assignments saved',
      );
      resetMutation();
      await onUpdate();
      reload();
    } catch (error) {
      const message = mutationErrorMessage(error);
      toast.error(message);
      if (isKnowledgeConflict(error)) {
        resetMutation();
        reload();
      }
    } finally {
      setMutationBusy(false);
    }
  };

  const handleRefresh = async (rootKnowledgeId: string) => {
    if (readOnly || !technicalRead) return;
    const fingerprint = `${rootKnowledgeId}:${technicalRead.revision}`;
    if (refreshIntentRef.current?.fingerprint !== fingerprint) {
      refreshIntentRef.current = {
        fingerprint,
        idempotencyKey: uuidv4(),
      };
    }
    const idempotencyKey = refreshIntentRef.current.idempotencyKey;
    setRefreshingRootId(rootKnowledgeId);
    try {
      await apiRef.current.refreshCardKnowledgeAssignments(card.id, {
        contract_version: 2,
        knowledge_ids: [rootKnowledgeId],
        idempotency_key: idempotencyKey,
        expected_revision: technicalRead.revision,
      });
      refreshIntentRef.current = null;
      toast.success('Knowledge snapshot refreshed');
      await onUpdate();
      reload();
    } catch (error) {
      toast.error(mutationErrorMessage(error));
      reload();
    } finally {
      setRefreshingRootId(null);
    }
  };

  return (
    <div className="modal-body space-y-4">
      <div className="px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 text-sm text-gray-600 dark:text-gray-300 flex items-center gap-2">
        <Shield size={15} />
        {readOnly
          ? 'Knowledge is read-only while this card is Rejected. Move it to In Progress before changing propagation decisions.'
          : 'Knowledge content is read-only; propagation decisions are governed below.'}
      </div>

      {loading && (
        <div
          className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-gray-300 py-6 text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400"
          role="status"
        >
          <RefreshCw size={15} className="animate-spin" />
          Loading Knowledge context…
        </div>
      )}

      {!loading && loadError && (
        <div
          className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300"
          role="alert"
        >
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span className="flex-1">{loadError}</span>
          <button
            type="button"
            onClick={reload}
            className="font-medium underline underline-offset-2"
          >
            Retry
          </button>
        </div>
      )}

      {!loading && technicalRead && (
        <section
          className="rounded-xl border border-gray-200 p-3 dark:border-gray-700"
          aria-labelledby="current-knowledge-selection-title"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3
              id="current-knowledge-selection-title"
              className="text-sm font-semibold text-gray-900 dark:text-white"
            >
              Current governed selection
            </h3>
            <div className="flex items-center gap-1.5 text-xs">
              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                {technicalRead.selection_state || 'legacy'}
              </span>
              <span className="text-gray-400">revision {technicalRead.revision}</span>
            </div>
          </div>

          {technicalRead.assignments.length === 0 ? (
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              No active governed assignments.
            </p>
          ) : (
            <div className="mt-2 space-y-1.5">
              {technicalRead.assignments.map((assignment) => (
                <div
                  key={`${assignment.root_knowledge_id}:${assignment.mode}`}
                  className="flex flex-wrap items-center gap-2 rounded-lg bg-gray-50 px-2.5 py-2 text-xs dark:bg-gray-900"
                  data-testid={`knowledge-assignment-${assignment.root_knowledge_id}`}
                >
                  <span className="min-w-0 flex-1 truncate font-medium text-gray-800 dark:text-gray-200">
                    {assignmentTitle(assignment, candidates)}
                  </span>
                  <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-200">
                    {assignment.mode}
                  </span>
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600 dark:bg-slate-700 dark:text-slate-200">
                    {ORIGIN_CLASS_LABELS[assignment.origin_class]
                      || assignment.origin_class}
                  </span>
                  {assignment.stale && (
                    <span className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                      stale
                    </span>
                  )}
                  {!readOnly && assignment.mode === 'snapshot' && assignment.stale && (
                    <button
                      type="button"
                      onClick={() => void handleRefresh(assignment.root_knowledge_id)}
                      disabled={refreshingRootId !== null || mutationBusy}
                      className="inline-flex items-center gap-1 rounded border border-amber-300 px-1.5 py-0.5 font-medium text-amber-800 hover:bg-amber-50 disabled:opacity-50 dark:border-amber-700 dark:text-amber-200 dark:hover:bg-amber-950/30"
                    >
                      <RefreshCw
                        size={11}
                        className={
                          refreshingRootId === assignment.root_knowledge_id
                            ? 'animate-spin'
                            : ''
                        }
                      />
                      Refresh
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      <KnowledgeWorkspace
        boardId={card.board_id}
        entityType="card"
        entityId={card.id}
        refreshKey={reloadGeneration}
        fallbackItems={card.knowledge_bases || []}
      />

      {!loading && technicalRead && !readOnly && (
        <>
          <KnowledgePropagationSelector
            items={candidates}
            value={choice}
            onChange={updateChoice}
            disabled={mutationBusy || refreshingRootId !== null}
            title="Change propagation decision"
            description="The picker starts empty for every operation. Choose a mode and only the roots relevant to this card."
            testId="card-knowledge-propagation"
          />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-gray-500 dark:text-gray-400">
              DROP and explicit empty do not mark Resource Gate as N/A.
            </p>
            <button
              type="button"
              onClick={() => void handleSaveDecision()}
              disabled={
                mutationBusy
                || refreshingRootId !== null
                || choice.action === 'omitted'
                || !isKnowledgePropagationChoiceValid(choice)
              }
              className="btn btn-primary disabled:cursor-not-allowed disabled:opacity-50"
            >
              {mutationBusy ? 'Saving…' : 'Save explicit decision'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
