import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ListChecks,
  Loader2,
  Play,
  RefreshCw,
  Send,
} from 'lucide-react';
import toast from 'react-hot-toast';

import { PreviousResultsSection } from '@/components/validation-cycle/ValidationCyclePrimitives';
import { useDashboardApi } from '@/services/api';
import type {
  ChecklistExecutionStartResult,
  ChecklistItemResult,
  ChecklistOutcome,
  ChecklistReceiptPage,
  ChecklistSpecState,
  ChecklistTemplate,
} from '@/types';

interface SpecChecklistPanelProps {
  boardId: string;
  specId: string;
  expectedSpecVersion: number;
  expectedSpecEdition: number;
  canRead?: boolean;
  canExecute?: boolean;
  /** Checklist writes are allowed only while the Spec is in Validation. */
  validationStageActive?: boolean;
  showHistory?: boolean;
  /** Suppresses the repeated title inside the unified Spec workspace. */
  embedded?: boolean;
  onStateChange?: (state: ChecklistSpecState | null) => void;
}

type ItemDraft = {
  outcome: ChecklistOutcome | '';
  anchor: string;
  rationale: string;
};

const STATUS_STYLES: Record<ChecklistSpecState['status'], string> = {
  off: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
  not_started:
    'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
  current: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  stale: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-200',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
};

const OUTCOME_STYLES: Record<ChecklistOutcome, string> = {
  pass: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  fail: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  not_applicable:
    'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
};

export function SpecChecklistPanel({
  boardId,
  specId,
  expectedSpecVersion,
  expectedSpecEdition,
  canRead = true,
  canExecute = true,
  validationStageActive = true,
  showHistory = false,
  embedded = false,
  onStateChange,
}: SpecChecklistPanelProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;

  const [state, setState] = useState<ChecklistSpecState | null>(null);
  const [template, setTemplate] = useState<ChecklistTemplate | null>(null);
  const [execution, setExecution] =
    useState<ChecklistExecutionStartResult | null>(null);
  const [drafts, setDrafts] = useState<Record<string, ItemDraft>>({});
  const [history, setHistory] = useState<ChecklistReceiptPage | null>(null);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);
  const historyLoadKeyRef = useRef<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState<25 | 50 | 100>(25);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const loadState = useCallback(async () => {
    if (!canRead) {
      setState(null);
      onStateChangeRef.current?.(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      if (!Number.isInteger(expectedSpecEdition) || expectedSpecEdition < 1) {
        throw new Error('A positive validation edition is required.');
      }
      const [resolvedState, templates] = await Promise.all([
        apiRef.current.getSpecChecklistState(boardId, specId),
        apiRef.current.listChecklistTemplates(),
      ]);
      if (
        resolvedState.subject.board_id !== boardId
        || resolvedState.subject.spec_id !== specId
        || resolvedState.subject.spec_edition !== expectedSpecEdition
        || (resolvedState.current_receipt !== null && (
          !Number.isInteger(resolvedState.current_receipt.spec_edition)
          || resolvedState.current_receipt.spec_edition < 1
          || resolvedState.current_receipt.spec_edition > expectedSpecEdition
          || resolvedState.current_receipt.source !== 'native'
          || resolvedState.current_receipt.board_id !== boardId
          || resolvedState.current_receipt.spec_id !== specId
        ))
      ) {
        throw new Error('The checklist state does not match this subject edition.');
      }
      const resolvedTemplate =
        templates.items.find(
          (item) =>
            item.version === resolvedState.binding.template_version_id,
        ) ?? null;
      setState(resolvedState);
      setTemplate(resolvedTemplate);
      onStateChangeRef.current?.(resolvedState);
    } catch (error: any) {
      setState(null);
      onStateChangeRef.current?.(null);
      toast.error(error?.message || 'Failed to load the curated checklist');
    } finally {
      setLoading(false);
    }
  }, [
    boardId,
    canRead,
    expectedSpecEdition,
    specId,
  ]);

  useEffect(() => {
    void loadState();
  }, [loadState]);

  useEffect(() => {
    setExecution(null);
    setDrafts({});
  }, [boardId, expectedSpecEdition, specId]);

  useEffect(() => {
    setHistory(null);
    setHistoryError(null);
    setHistoryExpanded(false);
    setOffset(0);
    historyLoadKeyRef.current = null;
  }, [boardId, expectedSpecEdition, showHistory, specId]);

  useEffect(() => {
    const shouldLoad = showHistory && historyExpanded;
    if (!canRead || !shouldLoad) return undefined;
    const loadKey = [boardId, specId, expectedSpecEdition, offset, limit, historyRefreshKey].join(':');
    if (historyLoadKeyRef.current === loadKey) return undefined;
    let cancelled = false;
    setHistoryLoading(true);
    setHistoryError(null);
    apiRef.current.listChecklistExecutions(boardId, specId, offset, limit)
      .then((resolvedHistory) => {
        if (cancelled) return;
        if (resolvedHistory.items.some(({ receipt }) => (
          !Number.isInteger(receipt.spec_edition)
          || receipt.spec_edition < 1
          || receipt.spec_edition > expectedSpecEdition
          || receipt.source !== 'native'
          || receipt.board_id !== boardId
          || receipt.spec_id !== specId
        ))) {
          throw new Error('The previous checklist results do not match this subject edition.');
        }
        historyLoadKeyRef.current = loadKey;
        setHistory(resolvedHistory);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setHistoryError(
          error instanceof Error
            ? error.message
            : 'Previous checklist results could not be loaded.',
        );
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => { cancelled = true; };
  }, [
    boardId,
    canRead,
    expectedSpecEdition,
    historyExpanded,
    historyRefreshKey,
    limit,
    offset,
    showHistory,
    specId,
  ]);

  const refreshAfterMutation = useCallback(async () => {
    historyLoadKeyRef.current = null;
    setHistoryRefreshKey((value) => value + 1);
    await loadState();
  }, [loadState]);

  const start = async () => {
    if (
      !state
      || state.binding.mode === 'off'
      || !validationStageActive
      || !canExecute
      || starting
    ) return;
    setStarting(true);
    try {
      const started = await apiRef.current.startChecklistExecution(
        boardId,
        specId,
        {
          spec_edition: expectedSpecEdition,
          expected_spec_version: expectedSpecVersion,
          binding_version: state.binding.version,
        },
      );
      if (started.spec_edition !== expectedSpecEdition) {
        throw new Error('The checklist execution does not match this subject edition.');
      }
      setExecution(started);
      setDrafts(
        Object.fromEntries(
          (template?.items ?? []).map((item) => [
            item.item_id,
            { outcome: '', anchor: '', rationale: '' },
          ]),
        ),
      );
    } catch (error: any) {
      toast.error(error?.message || 'Failed to start checklist execution');
      await loadState();
    } finally {
      setStarting(false);
    }
  };

  const updateDraft = (itemId: string, patch: Partial<ItemDraft>) => {
    setDrafts((current) => {
      const existing = current[itemId] ?? {
        outcome: '',
        anchor: '',
        rationale: '',
      };
      return {
        ...current,
        [itemId]: { ...existing, ...patch },
      };
    });
  };

  const itemComplete = (itemId: string): boolean => {
    const draft = drafts[itemId];
    return Boolean(
      draft?.outcome &&
        draft.anchor.trim() &&
        (draft.outcome !== 'not_applicable' || draft.rationale.trim()),
    );
  };

  const allItemsComplete = Boolean(
    template?.items.length === 10 &&
      template.items.every((item) => itemComplete(item.item_id)),
  );

  const submit = async () => {
    if (
      !state || !execution || !template || !allItemsComplete || submitting
      || !canExecute || !validationStageActive
      || execution.spec_edition !== expectedSpecEdition
    ) return;
    setSubmitting(true);
    try {
      const results: ChecklistItemResult[] = template.items.map((item) => {
        const draft = drafts[item.item_id];
        return {
          item_id: item.item_id,
          outcome: draft.outcome as ChecklistOutcome,
          anchor: draft.anchor.trim(),
          rationale: draft.rationale.trim() || null,
        };
      });
      await apiRef.current.submitChecklistExecution(
        boardId,
        specId,
        {
          spec_edition: expectedSpecEdition,
          expected_spec_version: expectedSpecVersion,
          execution_id: execution.execution_id,
          item_results: results,
        },
      );
      toast.success('Checklist result recorded');
      setExecution(null);
      setDrafts({});
      await refreshAfterMutation();
    } catch (error: any) {
      toast.error(error?.message || 'Failed to submit checklist');
      await loadState();
    } finally {
      setSubmitting(false);
    }
  };

  if (!canRead) {
    return (
      <section className="rounded-lg border border-gray-200 p-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
        The `spec.checklist.read` permission is required to view this checklist.
      </section>
    );
  }

  if (loading && !state) {
    return (
      <section className="flex items-center gap-2 rounded-lg border border-gray-200 p-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
        <Loader2 size={13} className="animate-spin" />
        Loading curated checklist…
      </section>
    );
  }

  if (!state || !template) {
    return (
      <section className="rounded-lg border border-red-200 bg-red-50 p-4 text-xs text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300">
        Checklist readiness is unavailable. Validation remains fail-closed.
      </section>
    );
  }

  const lifecycleEdition = expectedSpecEdition;
  const currentReceipt = state.subject.spec_edition === lifecycleEdition
    && state.current_receipt?.spec_edition === lifecycleEdition
    && state.currentness?.current === true
    && state.status !== 'stale'
    ? state.current_receipt
    : null;
  const displayStatus = state.binding.mode === 'off'
    ? 'off'
    : !currentReceipt
      ? 'not_started'
      : state.status === 'failed'
        ? 'failed'
        : 'current';
  const gateAllowed = Boolean(currentReceipt && state.gate.allowed);
  const currentResultByItemId = new Map(
    (currentReceipt?.results ?? []).map((result) => [
      result.item_id,
      result,
    ]),
  );

  return (
    <section
      className="space-y-4 rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900/40"
      data-testid="spec-checklist-panel"
    >
      {!embedded && (
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-gray-800 dark:text-gray-100">
            <ListChecks size={13} />
            Curated Spec Checklist
          </h4>
          <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
            {`Current result for Edition ${lifecycleEdition}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`rounded px-2 py-1 text-[10px] font-bold uppercase ${STATUS_STYLES[displayStatus]}`}
            data-testid="checklist-state-status"
          >
            {displayStatus.replace('_', ' ')}
          </span>
          <button
            type="button"
            onClick={() => void loadState()}
            aria-label="Refresh checklist state"
            disabled={loading || starting || submitting}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-50 dark:hover:bg-gray-800"
          >
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>
      )}

      <div
        className={`rounded border p-3 ${
          gateAllowed
            ? 'border-green-200 bg-green-50 dark:border-green-800 dark:bg-green-900/20'
            : 'border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-900/20'
        }`}
      >
        <div className="flex items-start gap-2">
          {gateAllowed ? (
            <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-green-600" />
          ) : (
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-600" />
          )}
          <div>
            <p className="text-xs font-medium text-gray-800 dark:text-gray-100">
              {state.binding.mode === 'blocking'
                ? gateAllowed
                  ? 'Spec Validation gate is satisfied'
                  : 'Spec Validation is blocked'
                : state.binding.mode === 'advisory'
                  ? 'Advisory evidence'
                  : 'Checklist policy is off'}
            </p>
            <p className="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">
              {gateAllowed
                ? 'The checklist result is available for this edition.'
                : 'Complete the checklist for this edition before submitting validation.'}
            </p>
          </div>
        </div>
      </div>

      {currentReceipt && !execution && (
        <div className="rounded border border-gray-100 bg-gray-50 p-3 dark:border-gray-800 dark:bg-gray-800/60">
          <div className="flex items-center justify-between gap-2">
            <p className="text-[11px] font-semibold text-gray-700 dark:text-gray-200">
              Current result
            </p>
          </div>
          <div className="mt-2 grid grid-cols-3 gap-2 text-center text-[10px]">
            {(['pass', 'fail', 'not_applicable'] as ChecklistOutcome[]).map(
              (outcome) => (
                <div
                  key={outcome}
                  className="rounded bg-white p-1.5 dark:bg-gray-900"
                >
                  <span className="block font-bold text-gray-800 dark:text-gray-100">
                    {
                      currentReceipt.results.filter(
                        (item) => item.outcome === outcome,
                      ).length
                    }
                  </span>
                  <span className="text-gray-400">
                    {outcome.replace('_', ' ')}
                  </span>
                </div>
              ),
            )}
          </div>
        </div>
      )}

      {!execution && (
        <div
          className="space-y-2"
          data-testid={
            currentReceipt
              ? 'checklist-receipt-results'
              : 'checklist-template-preview'
          }
        >
          <div>
            <h5 className="text-[11px] font-semibold uppercase text-gray-600 dark:text-gray-300">
              Checklist items
            </h5>
            <p className="mt-0.5 text-[10px] text-gray-400">
              {currentReceipt
                ? 'Recorded outcomes and evidence for the current edition.'
                : 'Immutable template preview. Start the checklist to record outcomes and evidence.'}
            </p>
          </div>
          {template.items.map((item, index) => {
            const result = currentResultByItemId.get(item.item_id);
            return (
              <div
                key={item.item_id}
                className="rounded border border-gray-200 p-3 dark:border-gray-700"
              >
                <div className="flex items-start gap-2">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-gray-100 text-[10px] font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                    {index + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <p className="text-xs font-semibold text-gray-800 dark:text-gray-100">
                        {item.title_en}
                      </p>
                      {result ? (
                        <span
                          className={`rounded px-2 py-0.5 text-[9px] font-bold uppercase ${OUTCOME_STYLES[result.outcome]}`}
                        >
                          {result.outcome.replace('_', ' ')}
                        </span>
                      ) : (
                        <span className="rounded bg-gray-100 px-2 py-0.5 text-[9px] font-medium text-gray-500 dark:bg-gray-800 dark:text-gray-400">
                          Not evaluated
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[10px] leading-4 text-gray-500 dark:text-gray-400">
                      {item.description_en}
                    </p>
                    {result ? (
                      <div className="mt-2 space-y-1 text-[10px]">
                        <p className="break-words text-gray-600 dark:text-gray-300">
                          <span className="font-semibold">Anchor:</span>{' '}
                          {result.anchor}
                        </p>
                        {result.rationale && (
                          <p className="break-words text-gray-500 dark:text-gray-400">
                            <span className="font-semibold">Rationale:</span>{' '}
                            {result.rationale}
                          </p>
                        )}
                      </div>
                    ) : item.allow_na ? (
                      <p className="mt-1 text-[9px] text-gray-400">
                        Not applicable is allowed with a rationale.
                      </p>
                    ) : null}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {!execution &&
        state.binding.mode !== 'off' &&
        (!validationStageActive ? (
          <p className="text-[11px] text-gray-500 dark:text-gray-400">
            Checklist results can be recorded only during this Spec&apos;s validation stage.
          </p>
        ) : canExecute ? (
          <button
            type="button"
            onClick={() => void start()}
            disabled={starting}
            className="inline-flex items-center gap-1.5 rounded bg-violet-600 px-3 py-2 text-xs font-medium text-white hover:bg-violet-700 disabled:bg-gray-400"
          >
            {starting ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <Play size={13} />
            )}
            {currentReceipt ? 'Run checklist again' : 'Start checklist'}
          </button>
        ) : (
          <p className="text-[11px] text-gray-500 dark:text-gray-400">
            The `spec.checklist.execute` permission is required to run the
            checklist.
          </p>
        ))}

      {execution && (
        <div className="space-y-3" data-testid="checklist-execution-form">
          <div className="rounded bg-violet-50 px-3 py-2 text-[10px] text-violet-700 dark:bg-violet-900/20 dark:text-violet-200">
            {`Checklist in progress for Edition ${lifecycleEdition}. Submit all 10 ordered results together.`}
          </div>
          {template.items.map((item, index) => {
            const draft = drafts[item.item_id] ?? {
              outcome: '',
              anchor: '',
              rationale: '',
            };
            return (
              <div
                key={item.item_id}
                className="rounded border border-gray-200 p-3 dark:border-gray-700"
              >
                <div className="flex items-start gap-2">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-gray-100 text-[10px] font-bold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                    {index + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-semibold text-gray-800 dark:text-gray-100">
                      {item.title_en}
                    </p>
                    <p className="mt-0.5 text-[10px] leading-4 text-gray-500 dark:text-gray-400">
                      {item.description_en}
                    </p>
                    <div className="mt-2 grid gap-2 sm:grid-cols-[150px_minmax(0,1fr)]">
                      <select
                        aria-label={`${item.title_en} outcome`}
                        value={draft.outcome}
                        onChange={(event) =>
                          updateDraft(item.item_id, {
                            outcome: event.target.value as
                              | ChecklistOutcome
                              | '',
                          })
                        }
                        className="rounded border border-gray-300 bg-white px-2 py-1.5 text-xs dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
                      >
                        <option value="">Choose outcome</option>
                        <option value="pass">Pass</option>
                        <option value="fail">Fail</option>
                        {item.allow_na && (
                          <option value="not_applicable">Not applicable</option>
                        )}
                      </select>
                      <input
                        aria-label={`${item.title_en} anchor`}
                        value={draft.anchor}
                        onChange={(event) =>
                          updateDraft(item.item_id, {
                            anchor: event.target.value,
                          })
                        }
                        placeholder="Required evidence anchor (FR/AC/section)"
                        className="rounded border border-gray-300 bg-white px-2 py-1.5 text-xs dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
                      />
                    </div>
                    {(draft.outcome === 'not_applicable' ||
                      draft.outcome === 'fail') && (
                      <textarea
                        aria-label={`${item.title_en} rationale`}
                        value={draft.rationale}
                        onChange={(event) =>
                          updateDraft(item.item_id, {
                            rationale: event.target.value,
                          })
                        }
                        rows={2}
                        placeholder={
                          draft.outcome === 'not_applicable'
                            ? 'Required N/A rationale'
                            : 'Optional failure rationale'
                        }
                        className="mt-2 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-xs dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
                      />
                    )}
                  </div>
                </div>
              </div>
            );
          })}
          <div className="flex items-center justify-between gap-3">
            <span className="text-[10px] text-gray-500 dark:text-gray-400">
              {template.items.filter((item) => itemComplete(item.item_id)).length}
              /10 complete
            </span>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!allItemsComplete || submitting || !canExecute || !validationStageActive}
              className="inline-flex items-center gap-1.5 rounded bg-violet-600 px-3 py-2 text-xs font-medium text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:bg-gray-400"
            >
              {submitting ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Send size={13} />
              )}
              Submit complete checklist
            </button>
          </div>
        </div>
      )}

      {showHistory && (
        <PreviousResultsSection
          expanded={historyExpanded}
          onToggle={() => setHistoryExpanded((value) => !value)}
          count={history
            ? Math.max(
                0,
                history.total_filtered
                  - (currentReceipt ? 1 : 0),
              )
            : undefined}
          title="Previous checklist results"
          description="Earlier attempts and editions are loaded only when opened."
          testId="checklist-previous-results"
        >
          {historyLoading && !history ? (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              Loading previous checklist results…
            </p>
          ) : historyError ? (
            <p role="alert" className="text-xs text-red-700 dark:text-red-300">
              Previous checklist results could not be loaded. {historyError}
            </p>
          ) : !history ? (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              Open this section to load previous checklist results.
            </p>
          ) : history.items.filter(
              (view) => view.receipt.id !== currentReceipt?.id,
            ).length === 0 ? (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              No previous checklist results are available.
            </p>
          ) : (
            <div className="space-y-2">
              {history.items
                .filter((view) =>
                  view.receipt.id !== currentReceipt?.id
                )
                .map((view) => (
                  <div
                    key={view.receipt.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-surface-200 bg-surface-50/70 p-3 text-xs dark:border-surface-700 dark:bg-surface-800/40"
                  >
                    <span>
                      <span className="font-semibold text-surface-800 dark:text-surface-100">
                        {`Edition ${view.receipt.spec_edition}`}
                      </span>
                      <span className="ml-2 text-surface-500 dark:text-surface-400">
                        {new Date(view.receipt.created_at).toLocaleString()}
                      </span>
                    </span>
                    <span className={view.receipt.outcome === 'pass'
                      ? 'font-semibold text-emerald-700 dark:text-emerald-300'
                      : 'font-semibold text-red-700 dark:text-red-300'}
                    >
                      {view.receipt.outcome === 'pass' ? 'Passed' : 'Failed'}
                    </span>
                  </div>
                ))}
            </div>
          )}
          {history && (
            <div className="flex items-center justify-end gap-2">
            <select
              aria-label="Checklist history page size"
              value={limit}
              onChange={(event) => {
                setLimit(Number(event.target.value) as 25 | 50 | 100);
                setOffset(0);
              }}
              className="rounded border border-gray-300 bg-white px-1.5 py-1 text-[10px] dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
            >
              <option value={25}>25</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
            </select>
              <button
                type="button"
                aria-label="Previous checklist results page"
                onClick={() => setOffset(Math.max(0, offset - limit))}
                disabled={offset === 0}
                className="rounded p-1 text-surface-500 hover:bg-surface-100 disabled:opacity-30 dark:hover:bg-surface-800"
              >
                <ChevronLeft size={13} />
              </button>
              <span className="text-[10px] text-surface-400">
                {history.total_filtered === 0 ? 0 : offset + 1}–
                {Math.min(offset + limit, history.total_filtered)}
              </span>
              <button
                type="button"
                aria-label="Next checklist results page"
                onClick={() => setOffset(offset + limit)}
                disabled={offset + limit >= history.total_filtered}
                className="rounded p-1 text-surface-500 hover:bg-surface-100 disabled:opacity-30 dark:hover:bg-surface-800"
              >
                <ChevronRight size={13} />
              </button>
            </div>
          )}
        </PreviousResultsSection>
      )}
    </section>
  );
}
