/**
 * CardKnowledgeTab - read-only Knowledge Base snapshots for a card/task.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  BookOpen,
  ChevronDown,
  ChevronUp,
  Download,
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
} from '@/types';
import { MarkdownContent } from '@/components/shared/MarkdownContent';
import { AuthenticatedFetchError } from '@/lib/authFetch';
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
  onUpdate: (kbs: any[]) => Promise<void>;
  onBusyChange?: (busy: boolean) => void;
}

function isSpecSnapshot(kb: any): boolean {
  const source = String(kb.source || '');
  return source === 'spec' || source.startsWith('copied_from_spec:') || Boolean(kb.source_kb_id);
}

function effectiveKnowledgeToCardSnapshot(item: EffectiveResourceItem): any | null {
  const resource = item.resource && typeof item.resource === 'object'
    ? item.resource as Record<string, unknown>
    : item as Record<string, unknown>;
  const id = String(item.id || resource.id || '');
  if (!id) return null;
  return {
    id,
    title: String(resource.title || item.title || 'Inherited knowledge'),
    description: typeof resource.description === 'string' ? resource.description : null,
    content: typeof resource.content === 'string' ? resource.content : '',
    mime_type: typeof resource.mime_type === 'string' ? resource.mime_type : 'text/markdown',
    inherited: item.inherited,
    read_only: item.read_only,
    source_entity_type: item.source_entity_type ?? item.provenance?.source_entity_type ?? null,
    source_entity_id: item.source_entity_id ?? item.provenance?.source_entity_id ?? null,
    source_entity_title: item.source_entity_title ?? item.provenance?.source_entity_title ?? null,
    source_id: item.source_id ?? resource.source_id ?? null,
    source_kb_id: item.source_kb_id ?? resource.source_kb_id ?? null,
    root_source_kb_id:
      item.ref?.root_resource_id
      ?? item.root_source_kb_id
      ?? resource.root_source_kb_id
      ?? null,
    knowledge_assignment_mode: item.ref?.knowledge_assignment_mode ?? null,
    knowledge_assignment_state: item.ref?.knowledge_assignment_state ?? null,
    knowledge_assignment_stale: item.ref?.knowledge_assignment_stale ?? false,
    origin_class: item.ref?.origin_class ?? null,
  };
}

function knowledgeIdentityValues(kb: any): string[] {
  return [
    kb?.id,
    kb?.source_id,
    kb?.source_kb_id,
    kb?.root_source_kb_id,
    kb?.source_ref,
    kb?.source,
  ]
    .filter((value): value is string | number => value !== null && value !== undefined && value !== '')
    .map((value) => String(value));
}

function sourceLabel(kb: any): string {
  const type = kb.source_entity_type || 'source';
  const title = kb.source_entity_title || kb.source_entity_id || 'parent';
  return `${type}: ${title}`;
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
  onBusyChange,
}: CardKnowledgeTabProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  const [effectiveItems, setEffectiveItems] = useState<EffectiveResourceItem[]>([]);
  const [sourceEffectiveItems, setSourceEffectiveItems] = useState<
    EffectiveResourceItem[]
  >([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
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
  const cardKBs: any[] = useMemo(() => {
    const direct = card.knowledge_bases || [];
    const directIds = new Set(direct.flatMap(knowledgeIdentityValues));
    const inherited = effectiveItems
      .filter((item) => item.inherited)
      .map(effectiveKnowledgeToCardSnapshot)
      .filter((item): item is any => Boolean(item))
      .filter((item) => !knowledgeIdentityValues(item).some((value) => directIds.has(value)));
    return [...direct, ...inherited];
  }, [card.knowledge_bases, effectiveItems]);

  const candidates = useMemo<KnowledgePropagationCandidate[]>(() => {
    const physical = specKnowledgeBases.map(physicalKnowledgeCandidate);
    const effective = sourceEffectiveItems
      .map(effectiveKnowledgeCandidate)
      .filter((item): item is KnowledgePropagationCandidate => item !== null);
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
  }, [sourceEffectiveItems, specKnowledgeBases, technicalRead]);

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
        apiRef.current.getEffectiveResources(card.board_id, 'card', card.id),
      ),
      Promise.resolve().then(() =>
        apiRef.current.getCardKnowledgeAssignments(card.id),
      ),
      Promise.resolve().then(async () => {
        if (!card.spec_id) return [] as EffectiveResourceItem[];
        const response = await apiRef.current.getEffectiveResources(
          card.board_id,
          'spec',
          card.spec_id,
        );
        return response.resources.knowledge_base || [];
      }),
    ]).then(([effectiveResult, technicalResult, sourceResult]) => {
      if (cancelled) return;
      const errors: string[] = [];
      if (effectiveResult.status === 'fulfilled') {
        setEffectiveItems(
          effectiveResult.value.resources.knowledge_base || [],
        );
      } else {
        setEffectiveItems([]);
        errors.push('effective Knowledge resources');
      }
      if (technicalResult.status === 'fulfilled') {
        setTechnicalRead(technicalResult.value);
      } else {
        setTechnicalRead(null);
        errors.push('governed assignments');
      }
      if (sourceResult.status === 'fulfilled') {
        setSourceEffectiveItems(sourceResult.value);
      } else {
        setSourceEffectiveItems([]);
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
    if (!technicalRead) return;
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
      reload();
    } catch (error) {
      toast.error(mutationErrorMessage(error));
      reload();
    } finally {
      setRefreshingRootId(null);
    }
  };

  const downloadMarkdown = (kb: any) => {
    const safeTitle = (kb.title || 'knowledge').replace(/[^A-Za-z0-9._-]+/g, '_');
    const filename = `${safeTitle || 'knowledge'}.md`;
    const body = `# ${kb.title || ''}\n\n> ${kb.description || ''}\n\n${kb.content || ''}\n`;
    const blob = new Blob([body], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="modal-body space-y-4">
      <div className="px-3 py-2 rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950 text-sm text-gray-600 dark:text-gray-300 flex items-center gap-2">
        <Shield size={15} />
        Knowledge content is read-only; propagation decisions are governed below.
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
                  {assignment.mode === 'snapshot' && assignment.stale && (
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

      {!loading && cardKBs.length === 0 ? (
        <div className="text-center py-8">
          <BookOpen size={32} className="mx-auto text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-sm text-gray-500 dark:text-gray-400">No knowledge bases</p>
          <p className="text-xs text-gray-400 mt-1">Choose an explicit reference or snapshot below when one is relevant.</p>
        </div>
      ) : !loading ? (
        <div className="space-y-2">
          {cardKBs.map((kb: any) => (
            <div key={kb.id} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
              <div
                data-testid={`kb-row-${kb.id}`}
                className="flex items-center gap-2 p-2.5 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                onClick={() => setExpandedId(expandedId === kb.id ? null : kb.id)}
              >
                <BookOpen size={14} className="text-gray-400 shrink-0" />
                <span className="text-sm font-medium text-gray-800 dark:text-gray-200 flex-1 truncate">{kb.title}</span>
                {isSpecSnapshot(kb) && (
                  <span className="text-[9px] px-1.5 py-0.5 bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300 rounded">from spec</span>
                )}
                {kb.inherited && (
                  <span className="text-[9px] px-1.5 py-0.5 bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-200 rounded">
                    from {sourceLabel(kb)}
                  </span>
                )}
                <span className="text-[9px] text-gray-400">{kb.mime_type || 'text/markdown'}</span>
                <button
                  onClick={(e) => { e.stopPropagation(); downloadMarkdown(kb); }}
                  className="text-gray-400 hover:text-emerald-600 p-0.5"
                  aria-label="Download markdown"
                  data-testid={`kb-download-${kb.id}`}
                >
                  <Download size={12} />
                </button>
                {expandedId === kb.id ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
              </div>
              {expandedId === kb.id && (
                <div className="px-3 pb-3 border-t border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-900/30">
                  <div className="pt-2 text-sm prose dark:prose-invert max-w-none">
                    <MarkdownContent content={kb.content} />
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      ) : null}

      {!loading && technicalRead && (
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
