import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleSlash2,
  ExternalLink,
  GitBranch,
  Link2,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react';

import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { useOpaqueCursorPager } from '@/hooks/useOpaqueCursorPager';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useDashboardApi } from '@/services/api';
import {
  SPEC_STATUSES,
  SPEC_STATUS_LABELS,
  type LookupOption,
  type SpecStatus,
} from '@/types';
import type {
  AddSpecDependencyRequest,
  RemoveSpecDependencyRequest,
  SpecDependencyMutationResponse,
  SpecDependencyDirection,
  SpecDependencyActiveStateFilter,
  SpecDependencyItem,
  SpecDependencyLineageFilter,
  SpecDependencyPage,
  SpecDependencyReadiness,
  SpecDependencySatisfactionFilter,
  SpecDependencySpecSummary,
} from '@/types/spec-dependencies';

const PAGE_LIMIT = 25;

interface DirectionFilters {
  satisfaction: SpecDependencySatisfactionFilter;
  retrospective: 'all' | 'retrospective' | 'planned';
  activeState: SpecDependencyActiveStateFilter;
  lineage: SpecDependencyLineageFilter;
  targetStatus: SpecStatus | '';
}

const INITIAL_FILTERS: DirectionFilters = {
  satisfaction: 'all',
  retrospective: 'all',
  activeState: 'active',
  lineage: 'all',
  targetStatus: '',
};

export interface SpecDependenciesTabProps {
  boardId: string;
  spec: {
    id: string;
    title: string;
    version: number;
    edition: number;
    status: SpecStatus;
    ideation_id: string | null;
  };
  readiness: SpecDependencyReadiness | null;
  readinessLoading: boolean;
  readinessError: string | null;
  canAdd: boolean;
  addDisabledReason?: string | null;
  canRemove: boolean;
  requestedDirection?: SpecDependencyDirection;
  directionRequestToken?: number;
  onRetryReadiness: () => void;
  onOpenSpec: (specId: string) => void;
  onMutated: (minimumVersion?: number) => Promise<void>;
  refreshKey?: number;
  readCurrent?: boolean;
  onAccessLost?: () => void;
}

/** Local dialog recovery over the existing dependency receipt contract. */
function useDependencySubmission<T extends AddSpecDependencyRequest | RemoveSpecDependencyRequest>({
  version, edition, readCurrent, send, refresh, close, onAccessLost,
}: {
  version: number; edition: number; readCurrent: boolean;
  send: (request: T) => Promise<SpecDependencyMutationResponse>;
  refresh: (minimumVersion?: number) => Promise<void>;
  close: () => void; onAccessLost?: () => void;
}) {
  const [base, setBase] = useState({ version, edition });
  const [attempt, setAttempt] = useState<{ request: T; confirmed: boolean; minimumVersion?: number } | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const changed = base.version !== version || base.edition !== edition;
  const reload = async () => {
    await refresh(attempt?.minimumVersion);
    if (live.current) setError(null);
  };
  const submit = async (request: T) => {
    if (busy.current || (!attempt?.confirmed && !readCurrent) || (!attempt && changed)) return;
    const original = attempt?.request ?? request;
    busy.current = true; setPending(true); setError(null);
    let confirmed = attempt?.confirmed ?? false;
    let minimumVersion = attempt?.minimumVersion;
    try {
      if (!confirmed) {
        const receipt = await send(original);
        if (!live.current) return;
        confirmed = true;
        minimumVersion = receipt.spec_version;
        setAttempt({ request: original, confirmed: true, minimumVersion });
      }
      await refresh(minimumVersion);
      if (live.current) close();
    } catch (caught) {
      if (!live.current) return;
      const denied = caught instanceof AuthenticatedFetchError && [401, 403, 404].includes(caught.status);
      if (denied) { onAccessLost?.(); return; }
      const rejected = caught instanceof AuthenticatedFetchError && caught.status >= 400 && caught.status < 500 && caught.status !== 408;
      setAttempt(confirmed || !rejected ? { request: original, confirmed, minimumVersion } : null);
      setError(confirmed ? 'Dependency change was saved. Reload its current state; the write will not be repeated.' : dependencyErrorMessage(caught));
      if (!confirmed && caught instanceof AuthenticatedFetchError && caught.status === 409) {
        try { await refresh(); } catch { /* Keep the original refusal and authored base. */ }
      }
    } finally {
      busy.current = false;
      if (live.current) setPending(false);
    }
  };
  return { base, changed, attempt, pending, error, setError, submit,
    review: () => { if (readCurrent && !attempt && !pending) setBase({ version, edition }); },
    reload: () => { void reload().catch(caught => {
      if (!live.current) return;
      if (caught instanceof AuthenticatedFetchError && [401, 403, 404].includes(caught.status)) onAccessLost?.();
      else setError(dependencyErrorMessage(caught));
    }); },
  };
}

function DependencyRecovery({ changed, uncertain, confirmed, pending, readCurrent, onReview, onReload }: {
  changed: boolean; uncertain: boolean; confirmed: boolean; pending: boolean; readCurrent: boolean;
  onReview: () => void; onReload: () => void;
}) {
  if (!changed && !uncertain && !confirmed && readCurrent) return null;
  return <div role="status" className="rounded-lg border border-amber-300 p-3 text-xs text-amber-700 dark:text-amber-300">
    <p>{confirmed ? 'The change is saved; finish by reloading the current state.' : uncertain
      ? 'The result is uncertain. Retry the original request to recover its receipt; its payload and base are preserved.'
      : changed ? 'The Spec changed. Your input is preserved; review the current dependency state before submitting.'
      : 'Current dependency state is unavailable. Reload before submitting.'}</p>
    {!readCurrent && <button type="button" className="btn btn-secondary mt-2 text-xs" disabled={pending} onClick={onReload}>Reload dependency state</button>}
    {changed && !uncertain && !confirmed && <button type="button" className="btn btn-secondary mt-2 text-xs" disabled={!readCurrent || pending} onClick={onReview}>Review dependency base</button>}
  </div>;
}

function removalBlockedMessage(reasonCode: string | null | undefined): string {
  switch (reasonCode) {
    case 'source_archived':
      return 'Restore the dependent Spec before removing this dependency.';
    case 'incoming_dependency_read_only':
      return 'Open the dependent Spec to remove this dependency.';
    case 'dependency_removed':
      return 'This dependency has already been removed.';
    default:
      return 'Removal is not authorized for this dependency.';
  }
}

function createClientKey(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  return uuid
    ? `${prefix}-${uuid}`
    : `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function timestamp(value: string | null | undefined): string {
  if (!value) return 'Not recorded';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function dependencyErrorFact(error: AuthenticatedFetchError, key: string): unknown {
  if (!error.details || typeof error.details !== 'object') return undefined;
  const details = error.details as Record<string, unknown>;
  const facts = details.facts;
  if (facts && typeof facts === 'object') {
    return (facts as Record<string, unknown>)[key];
  }
  return details[key];
}

function dependencyErrorMessage(error: unknown): string {
  if (error instanceof AuthenticatedFetchError) {
    if (error.code === 'spec_dependency_cycle') {
      return 'This dependency would create a cycle. Choose a different prerequisite.';
    }
    if (error.code === 'spec_dependency_self_reference') {
      return 'A Spec cannot depend on itself. Choose another prerequisite.';
    }
    if (error.code === 'dependency_target_unavailable') {
      return 'This prerequisite is unavailable on the current board. Choose another Spec.';
    }
    if (
      error.code === 'spec_dependency_state_conflict'
      && dependencyErrorFact(error, 'conflict_kind') === 'active_duplicate'
    ) {
      return 'This prerequisite is already an active dependency.';
    }
    if (
      error.code === 'spec_dependency_state_conflict'
      && dependencyErrorFact(error, 'conflict_kind') === 'idempotency_key_reuse'
    ) {
      return 'This request key was already used for a different dependency operation. Close the dialog and try again.';
    }
    if (
      error.code === 'spec_dependency_state_conflict'
      && /must already be Done/i.test(error.message)
    ) {
      return 'A dependency added after work has started must already be Done.';
    }
    if (error.code === 'spec_dependency_version_conflict') {
      return 'This spec changed while the dialog was open. Refresh it, review the latest version, and retry.';
    }
    if (error.code === 'invalid_cursor') {
      return 'This page cursor expired or no longer matches the active filters.';
    }
  }
  return error instanceof Error
    ? error.message
    : 'The dependency operation could not be completed.';
}

function classifyCursorError(error: unknown) {
  const invalidCursor = error instanceof AuthenticatedFetchError
    && (error.code === 'invalid_cursor' || error.status === 400);
  return {
    message: dependencyErrorMessage(error),
    restartRequired: invalidCursor,
  };
}

function statusTone(status: SpecStatus): string {
  switch (status) {
    case 'done':
      return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300';
    case 'in_progress':
      return 'bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300';
    case 'cancelled':
      return 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300';
    case 'validated':
      return 'bg-violet-100 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300';
    case 'approved':
      return 'bg-green-100 text-green-700 dark:bg-green-950/50 dark:text-green-300';
    case 'review':
      return 'bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300';
    default:
      return 'bg-surface-100 text-surface-700 dark:bg-surface-700 dark:text-surface-200';
  }
}

function filterKey(filters: DirectionFilters): string {
  return [
    filters.satisfaction,
    filters.retrospective,
    filters.activeState,
    filters.lineage,
    filters.targetStatus || 'all',
  ].join(':');
}

interface AddDependencyDialogProps {
  boardId: string;
  sourceSpecId: string;
  sourceSpecVersion: number;
  sourceSpecEdition: number;
  sourceStatus: SpecStatus;
  currentEditionStarted: boolean;
  onClose: () => void;
  onAdded: (minimumVersion?: number) => Promise<void>;
  readCurrent: boolean;
  refreshKey: number;
  onAccessLost?: () => void;
}

function AddDependencyDialog({
  boardId,
  sourceSpecId,
  sourceSpecVersion,
  sourceSpecEdition,
  sourceStatus,
  currentEditionStarted,
  onClose,
  onAdded,
  readCurrent,
  refreshKey,
  onAccessLost,
}: AddDependencyDialogProps) {
  const api = useDashboardApi();
  const { dialogRef, onKeyDown } = useDialogFocusTrap(
    true,
    '[data-dependency-dialog-focus]',
  );
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [candidates, setCandidates] = useState<LookupOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<LookupOption | null>(null);
  const accessLostRef = useRef(onAccessLost);
  accessLostRef.current = onAccessLost;
  const submission = useDependencySubmission<AddSpecDependencyRequest>({ version: sourceSpecVersion, edition: sourceSpecEdition,
    readCurrent, send: request => api.addSpecDependency(boardId, sourceSpecId, request), refresh: onAdded, close: onClose, onAccessLost });
  const { pending: submitting, error: submitError, setError: setSubmitError } = submission;
  const postStart = currentEditionStarted
    || ['in_progress', 'done', 'cancelled'].includes(sourceStatus);

  useEscapeToClose(onClose, {
    enabled: true,
    canClose: !submitting,
    priority: 80,
  });

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    void api.lookupSpecs(boardId, {
      search: debouncedSearch,
      statuses: postStart ? ['done'] : undefined,
      limit: 50,
      signal: controller.signal,
    }).then((page) => {
      if (controller.signal.aborted) return;
      setCandidates(page.items.filter((candidate) => candidate.id !== sourceSpecId));
    }).catch((error) => {
      if (!controller.signal.aborted) {
        setCandidates([]);
        setLoadError(dependencyErrorMessage(error));
        if (error instanceof AuthenticatedFetchError && [401, 403, 404].includes(error.status)) {
          setSelected(null);
          accessLostRef.current?.();
        }
      }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [api, boardId, debouncedSearch, postStart, sourceSpecId, refreshKey]);

  const submit = async () => {
    if (!selected || submitting || (!submission.attempt && !candidates.some(candidate => candidate.id === selected.id))) return;
    await submission.submit({ prerequisite_spec_id: selected.id,
      expected_spec_version: submission.base.version, expected_spec_edition: submission.base.edition,
      idempotency_key: createClientKey('spec-dependency-add') });
  };

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-spec-dependency-title"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="flex max-h-[85vh] w-full max-w-xl flex-col overflow-hidden rounded-xl bg-white shadow-2xl dark:bg-gray-800"
      >
        <header className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <div>
            <h2 id="add-spec-dependency-title" className="text-base font-semibold text-gray-900 dark:text-white">
              Add dependency
            </h2>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Choose a Spec that must be Done before this Spec can start.
            </p>
          </div>
          <button
            type="button"
            data-dependency-dialog-focus
            onClick={onClose}
            disabled={submitting}
            aria-label="Close add dependency dialog"
            className="rounded-md p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
          <DependencyRecovery changed={submission.changed} uncertain={Boolean(submission.attempt && !submission.attempt.confirmed)}
            confirmed={Boolean(submission.attempt?.confirmed)} pending={submitting} readCurrent={readCurrent}
            onReview={submission.review} onReload={submission.reload} />
          {postStart && (
            <div role="note" className="flex gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" />
              <p>
                Work has already started. Only a Done Spec is eligible and the new dependency will be recorded as retrospective.
              </p>
            </div>
          )}
          <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
            Search Specs
            <input
              type="search"
              disabled={submitting || Boolean(submission.attempt)}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search by title or ID"
              className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
            />
          </label>

          {loading && (
            <div role="status" className="flex items-center justify-center gap-2 py-8 text-sm text-gray-500 dark:text-gray-400">
              <Loader2 size={16} className="animate-spin" /> Loading eligible Specs…
            </div>
          )}
          {loadError && (
            <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
              {loadError}
            </div>
          )}
          {!loading && !loadError && candidates.length === 0 && (
            <p className="py-8 text-center text-sm text-gray-500 dark:text-gray-400">
              No eligible Specs match this search.
            </p>
          )}
          {!loading && candidates.length > 0 && (
            <fieldset className="space-y-2" disabled={submitting || Boolean(submission.attempt)}>
              <legend className="sr-only">Eligible dependency targets</legend>
              {candidates.map((candidate) => {
                const chosen = selected?.id === candidate.id;
                return (
                  <label
                    key={candidate.id}
                    className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors ${
                      chosen
                        ? 'border-blue-400 bg-blue-50 dark:border-blue-600 dark:bg-blue-950/30'
                        : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-700/40'
                    }`}
                  >
                    <input
                      type="radio"
                      name="dependency-target"
                      checked={chosen}
                      onChange={() => {
                        setSelected(candidate);
                        setSubmitError(null);
                      }}
                      className="mt-0.5"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-gray-900 dark:text-gray-100">
                        {candidate.title}
                      </span>
                      <span className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
                        <span className="font-mono">{candidate.id}</span>
                        <span className={`rounded-full px-2 py-0.5 font-medium ${statusTone(candidate.status as SpecStatus)}`}>
                          {SPEC_STATUS_LABELS[candidate.status as SpecStatus] ?? candidate.status}
                        </span>
                      </span>
                    </span>
                  </label>
                );
              })}
            </fieldset>
          )}
          {submitError && (
            <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
              {submitError}
            </div>
          )}
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-200 px-5 py-3 dark:border-gray-700">
          <p className="text-[11px] text-gray-500 dark:text-gray-400">
            The server validates same-board scope, duplicates, cycles and version concurrency.
          </p>
          <div className="flex gap-2">
            <button type="button" onClick={onClose} disabled={submitting} className="btn btn-secondary text-xs">
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!selected || submitting || (!submission.attempt?.confirmed && (!readCurrent || (!submission.attempt && (submission.changed || loading || Boolean(loadError) || !candidates.some(candidate => candidate.id === selected.id)))))}
              className="btn btn-primary inline-flex items-center gap-1.5 text-xs disabled:opacity-50"
            >
              {submitting && <Loader2 size={13} className="animate-spin" />}
              {submitting ? 'Saving…' : submission.attempt?.confirmed ? 'Reload saved dependency' : submission.attempt ? 'Retry original dependency request' : 'Add dependency'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

interface RemoveDependencyDialogProps {
  boardId: string;
  sourceSpecId: string;
  sourceSpecVersion: number;
  sourceSpecEdition: number;
  item: SpecDependencyItem;
  related: SpecDependencySpecSummary;
  onClose: () => void;
  onRemoved: (minimumVersion?: number) => Promise<void>;
  onSuccessfulClose: () => void;
  readCurrent: boolean;
  onAccessLost?: () => void;
}

function RemoveDependencyDialog({
  boardId,
  sourceSpecId,
  sourceSpecVersion,
  sourceSpecEdition,
  item,
  related,
  onClose,
  onRemoved,
  onSuccessfulClose,
  readCurrent,
  onAccessLost,
}: RemoveDependencyDialogProps) {
  const api = useDashboardApi();
  const { dialogRef, onKeyDown } = useDialogFocusTrap(
    true,
    '[data-dependency-remove-focus]',
  );
  const [reason, setReason] = useState('');
  const submission = useDependencySubmission<RemoveSpecDependencyRequest>({ version: sourceSpecVersion, edition: sourceSpecEdition,
    readCurrent, send: request => api.removeSpecDependency(boardId, sourceSpecId, item.id, request), refresh: onRemoved,
    close: () => { onClose(); onSuccessfulClose(); }, onAccessLost });
  const { pending: submitting, error, setError } = submission;

  useEscapeToClose(onClose, {
    enabled: true,
    canClose: !submitting,
    priority: 80,
  });

  const submit = async () => {
    const normalizedReason = reason.trim();
    if (!normalizedReason || submitting) return;
    await submission.submit({ expected_spec_version: submission.base.version, expected_spec_edition: submission.base.edition,
      idempotency_key: createClientKey('spec-dependency-remove'), reason: normalizedReason });
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 p-4">
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="remove-spec-dependency-title"
        aria-describedby="remove-spec-dependency-description"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="w-full max-w-lg overflow-hidden rounded-xl bg-white shadow-2xl dark:bg-gray-800"
      >
        <header className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <div>
            <h2 id="remove-spec-dependency-title" className="text-base font-semibold text-gray-900 dark:text-white">
              Remove dependency
            </h2>
            <p id="remove-spec-dependency-description" className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Remove the active dependency on “{related.title}”. The lifecycle record remains visible.
            </p>
          </div>
          <button
            type="button"
            data-dependency-remove-focus
            onClick={onClose}
            disabled={submitting}
            aria-label="Close remove dependency dialog"
            className="rounded-md p-1.5 text-gray-400 hover:bg-gray-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:hover:bg-gray-700"
          >
            <X size={18} />
          </button>
        </header>
        <div className="space-y-3 px-5 py-4">
          <DependencyRecovery changed={submission.changed} uncertain={Boolean(submission.attempt && !submission.attempt.confirmed)}
            confirmed={Boolean(submission.attempt?.confirmed)} pending={submitting} readCurrent={readCurrent}
            onReview={submission.review} onReload={submission.reload} />
          <div role="note" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
            Removing this dependency creates a tombstone and advances the authoritative Spec version.
          </div>
          <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
            Removal reason <span aria-hidden="true" className="text-red-500">*</span>
            <textarea
              disabled={submitting || Boolean(submission.attempt)}
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                setError(null);
              }}
              rows={4}
              required
              aria-required="true"
              placeholder="Explain why this dependency no longer applies…"
              className="mt-1 w-full resize-none rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100"
            />
          </label>
          {error && (
            <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
              {error}
            </div>
          )}
        </div>
        <footer className="flex justify-end gap-2 border-t border-gray-200 px-5 py-3 dark:border-gray-700">
          <button type="button" onClick={onClose} disabled={submitting} className="btn btn-secondary text-xs">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!reason.trim() || submitting || (!submission.attempt?.confirmed && (!readCurrent || (!submission.attempt && submission.changed)))}
            className="btn btn-danger inline-flex items-center gap-1.5 text-xs disabled:opacity-50"
          >
            {submitting && <Loader2 size={13} className="animate-spin" />}
            {submitting ? 'Saving…' : submission.attempt?.confirmed ? 'Reload saved dependency' : submission.attempt ? 'Retry original dependency request' : 'Remove dependency'}
          </button>
        </footer>
      </div>
    </div>
  );
}

interface DependencyRowProps {
  item: SpecDependencyItem;
  canRemove: boolean;
  onOpenSpec: (specId: string) => void;
  onRemove: (item: SpecDependencyItem, related: SpecDependencySpecSummary) => void;
}

function DependencyRow({
  item,
  canRemove,
  onOpenSpec,
  onRemove,
}: DependencyRowProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const related = item.related_spec;
  const canNavigate = item.capabilities?.can_navigate !== false;
  const sameIdeation = item.lineage === 'same_ideation';
  const removalAllowed = canRemove
    && item.active
    && item.capabilities?.can_remove === true;
  const removalBlockedReason = item.active && !removalAllowed
    ? removalBlockedMessage(item.capabilities?.remove_reason_code)
    : null;
  const removalBlockedReasonId = `dependency-${item.id}-remove-disabled-reason`;
  const tone = !item.active
    ? 'border-surface-200 bg-surface-50/70 dark:border-surface-700 dark:bg-surface-900/40'
    : item.satisfied && !related.archived
      ? 'border-emerald-200 bg-emerald-50/40 dark:border-emerald-900 dark:bg-emerald-950/20'
      : 'border-amber-200 bg-amber-50/50 dark:border-amber-900 dark:bg-amber-950/20';

  return (
    <article className={`rounded-lg border p-4 ${tone}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => onOpenSpec(related.id)}
              disabled={!canNavigate}
              className="min-w-0 truncate text-left text-sm font-semibold text-blue-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:text-gray-400 disabled:no-underline dark:text-blue-400"
              title={related.title}
            >
              {related.title}
            </button>
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${statusTone(related.status)}`}>
              {SPEC_STATUS_LABELS[related.status]}
            </span>
            {related.archived && (
              <span className="inline-flex items-center gap-1 rounded-full bg-gray-200 px-2 py-0.5 text-[10px] font-semibold text-gray-700 dark:bg-gray-700 dark:text-gray-200">
                <CircleSlash2 size={10} /> Archived
              </span>
            )}
            {!item.active ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-surface-200 px-2 py-0.5 text-[10px] font-semibold text-surface-700 dark:bg-surface-700 dark:text-surface-200">
                <CircleSlash2 size={10} /> Removed
              </span>
            ) : related.archived ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800 dark:bg-amber-900/60 dark:text-amber-300">
                <AlertTriangle size={10} /> Restore required
              </span>
            ) : item.satisfied ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 dark:bg-emerald-900/60 dark:text-emerald-300">
                <CheckCircle2 size={10} /> Satisfied
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800 dark:bg-amber-900/60 dark:text-amber-300">
                <AlertTriangle size={10} /> Unfinished
              </span>
            )}
          </div>
          <p className="mt-1 truncate font-mono text-[10px] text-gray-500 dark:text-gray-400" title={related.id}>
            {related.id}
          </p>
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-600 dark:text-gray-300">
            <span>{sameIdeation ? 'Same ideation' : 'Cross-ideation'}</span>
            <span>Introduced in Spec v{item.introduced_at_spec_version}</span>
            <span>{item.retrospective ? 'Retrospective' : 'Added before start'}</span>
            <span>
              {item.active
                ? 'Active'
                : `Removed in Spec v${item.removed_at_spec_version ?? 'unknown'} · ${timestamp(item.removed_at)}`}
            </span>
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-start gap-1 sm:items-end">
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => onOpenSpec(related.id)}
              disabled={!canNavigate}
              className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              <ExternalLink size={12} /> View spec
            </button>
            {item.active && (
              <button
                type="button"
                onClick={() => onRemove(item, related)}
                disabled={!removalAllowed}
                title={removalAllowed ? 'Remove dependency' : removalBlockedReason ?? undefined}
                aria-describedby={removalBlockedReason ? removalBlockedReasonId : undefined}
                className="inline-flex items-center gap-1 rounded-md border border-red-200 px-2.5 py-1.5 text-[11px] font-medium text-red-600 hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950/30"
              >
                <Trash2 size={12} /> Remove
              </button>
            )}
          </div>
          {removalBlockedReason && (
            <p
              id={removalBlockedReasonId}
              className="max-w-52 text-left text-[10px] leading-4 text-gray-500 sm:text-right dark:text-gray-400"
            >
              {removalBlockedReason}
            </p>
          )}
        </div>
      </div>

      {!item.active && (
        <div className="mt-3 border-t border-surface-200 pt-3 dark:border-surface-700">
          <p className="text-xs text-gray-700 dark:text-gray-300">
            <span className="font-semibold">Removal reason:</span>{' '}
            {item.removal_reason || 'No reason returned by the server.'}
          </p>
          <button
            type="button"
            aria-expanded={detailsOpen}
            aria-controls={`dependency-${item.id}-lifecycle`}
            onClick={() => setDetailsOpen((open) => !open)}
            className="mt-2 inline-flex items-center gap-1 text-[11px] font-medium text-blue-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-400"
          >
            {detailsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            View lifecycle details
          </button>
          {detailsOpen && (
            <dl id={`dependency-${item.id}-lifecycle`} className="mt-2 grid gap-2 text-[11px] text-gray-600 sm:grid-cols-2 dark:text-gray-300">
              <div><dt className="text-gray-400">Added</dt><dd>{timestamp(item.created_at)} by {item.created_by_name || item.created_by}</dd></div>
              <div>
                <dt className="text-gray-400">Removed</dt>
                <dd>
                  Spec v{item.removed_at_spec_version ?? 'unknown'} · {timestamp(item.removed_at)} by {item.removed_by_name || item.removed_by || 'Unknown actor'}
                </dd>
              </div>
            </dl>
          )}
        </div>
      )}
    </article>
  );
}

export function SpecDependenciesTab(props: SpecDependenciesTabProps) {
  return <SpecDependenciesWorkspace key={`${props.boardId}:${props.spec.id}`} {...props} />;
}

function SpecDependenciesWorkspace({
  boardId,
  spec,
  readiness,
  readinessLoading,
  readinessError,
  canAdd,
  addDisabledReason,
  canRemove,
  requestedDirection,
  directionRequestToken,
  onRetryReadiness,
  onOpenSpec,
  onMutated,
  refreshKey = 0,
  readCurrent = true,
  onAccessLost,
}: SpecDependenciesTabProps) {
  const api = useDashboardApi();
  const workspaceHeadingRef = useRef<HTMLHeadingElement>(null);
  const [accessDenied, setAccessDenied] = useState(false);
  const accessLostRef = useRef(onAccessLost);
  accessLostRef.current = onAccessLost;
  const loseAccess = useCallback(() => {
    setAccessDenied(true);
    setAddOpen(false);
    setRemoving(null);
    accessLostRef.current?.();
  }, []);
  const handleReadError = useCallback((error: unknown, signal: AbortSignal): never => {
    if (!signal.aborted && error instanceof AuthenticatedFetchError && [401, 403, 404].includes(error.status)) loseAccess();
    throw error;
  }, [loseAccess]);
  const [direction, setDirection] = useState<SpecDependencyDirection>(
    requestedDirection ?? 'depends_on',
  );
  const [filters, setFilters] = useState<Record<SpecDependencyDirection, DirectionFilters>>({
    depends_on: { ...INITIAL_FILTERS },
    required_by: { ...INITIAL_FILTERS },
  });
  const [addOpen, setAddOpen] = useState(false);
  const [removing, setRemoving] = useState<{
    item: SpecDependencyItem;
    related: SpecDependencySpecSummary;
  } | null>(null);

  useEffect(() => {
    if (requestedDirection) setDirection(requestedDirection);
  }, [directionRequestToken, requestedDirection]);
  useEffect(() => { if (!canAdd) setAddOpen(false); if (!canRemove) setRemoving(null); }, [canAdd, canRemove]);

  const outgoingFilters = filters.depends_on;
  const incomingFilters = filters.required_by;
  const loadOutgoing = useCallback((cursor: string | undefined, signal: AbortSignal) => (
    api.listSpecDependencies(boardId, spec.id, {
      direction: 'depends_on',
      satisfaction: outgoingFilters.satisfaction,
      retrospective: outgoingFilters.retrospective === 'all'
        ? undefined
        : outgoingFilters.retrospective === 'retrospective',
      active_state: outgoingFilters.activeState,
      lineage: outgoingFilters.lineage,
      related_statuses: outgoingFilters.targetStatus
        ? [outgoingFilters.targetStatus]
        : undefined,
      cursor,
      limit: PAGE_LIMIT,
      signal,
    }).catch(error => handleReadError(error, signal))
  ), [api, boardId, outgoingFilters, spec.id, handleReadError]);
  const loadIncoming = useCallback((cursor: string | undefined, signal: AbortSignal) => (
    api.listSpecDependencies(boardId, spec.id, {
      direction: 'required_by',
      satisfaction: incomingFilters.satisfaction,
      retrospective: incomingFilters.retrospective === 'all'
        ? undefined
        : incomingFilters.retrospective === 'retrospective',
      active_state: incomingFilters.activeState,
      lineage: incomingFilters.lineage,
      related_statuses: incomingFilters.targetStatus
        ? [incomingFilters.targetStatus]
        : undefined,
      cursor,
      limit: PAGE_LIMIT,
      signal,
    }).catch(error => handleReadError(error, signal))
  ), [api, boardId, incomingFilters, spec.id, handleReadError]);

  const outgoing = useOpaqueCursorPager<SpecDependencyItem, SpecDependencyPage>({
    enabled: !accessDenied && direction === 'depends_on',
    resetKey: `${boardId}:${spec.id}:depends_on:${filterKey(outgoingFilters)}:${refreshKey}`,
    loadPage: loadOutgoing,
    getItemKey: (item) => item.id,
    classifyError: classifyCursorError,
  });
  const incoming = useOpaqueCursorPager<SpecDependencyItem, SpecDependencyPage>({
    enabled: !accessDenied && direction === 'required_by',
    resetKey: `${boardId}:${spec.id}:required_by:${filterKey(incomingFilters)}:${refreshKey}`,
    loadPage: loadIncoming,
    getItemKey: (item) => item.id,
    classifyError: classifyCursorError,
  });
  const pager = direction === 'depends_on' ? outgoing : incoming;
  const effectiveReadiness = pager.page?.readiness ?? readiness;
  const archivedBlockerCount = effectiveReadiness?.archived_blocking_count ?? 0;
  const unfinishedBlockerCount = (
    effectiveReadiness?.unfinished_blocking_count ?? 0
  );
  const activeFilters = filters[direction];
  const dependencyReadCurrent = readCurrent && pager.loaded && !pager.loading && !pager.error;
  const canAddAuthoritatively = canAdd && dependencyReadCurrent;
  const addBlockedReasonId = `spec-${spec.id}-dependency-add-disabled-reason`;

  const setFilter = <K extends keyof DirectionFilters>(
    key: K,
    value: DirectionFilters[K],
  ) => {
    setFilters((current) => ({
      ...current,
      [direction]: { ...current[direction], [key]: value },
    }));
  };

  const refreshAfterMutation = async (minimumVersion?: number) => {
    await onMutated(minimumVersion);
    outgoing.restart();
    incoming.restart();
  };

  const total = pager.page?.total ?? pager.items.length;

  if (accessDenied) return <p role="alert">Dependency access is unavailable. Reopen the Spec after access is restored.</p>;

  return (
    <div className="space-y-4" data-testid="spec-dependencies-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3
            ref={workspaceHeadingRef}
            tabIndex={-1}
            className="flex items-center gap-2 rounded-sm text-sm font-semibold text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-white"
          >
            <GitBranch size={16} className="text-blue-500" /> Spec dependencies
          </h3>
          <p className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
            A prerequisite is satisfied only while its authoritative Spec is Done and available.
          </p>
        </div>
        {direction === 'depends_on' && (
          <div className="flex max-w-full flex-col items-start gap-1 sm:items-end">
            <button
              type="button"
              onClick={() => setAddOpen(true)}
              disabled={!canAddAuthoritatively}
              title={
                canAddAuthoritatively
                  ? 'Add dependency'
                  : addDisabledReason ?? 'Adding dependencies is not authorized.'
              }
              aria-describedby={!canAddAuthoritatively ? addBlockedReasonId : undefined}
              className="btn btn-primary inline-flex items-center gap-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Plus size={14} /> Add dependency
            </button>
            {!canAddAuthoritatively && (
              <p
                id={addBlockedReasonId}
                className="max-w-xs text-left text-[10px] leading-4 text-gray-500 sm:text-right dark:text-gray-400"
              >
                {addDisabledReason ?? 'Adding dependencies is not authorized.'}
              </p>
            )}
          </div>
        )}
      </div>

      {readinessLoading && (
        <div role="status" className="flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
          <Loader2 size={14} className="animate-spin" /> Loading dependency readiness…
        </div>
      )}
      {readinessError && !effectiveReadiness && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          <span>Dependency readiness is unavailable: {readinessError}</span>
          <button type="button" onClick={onRetryReadiness} className="inline-flex items-center gap-1 font-semibold hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500">
            <RefreshCw size={12} /> Retry
          </button>
        </div>
      )}
      {effectiveReadiness && effectiveReadiness.blocking_count > 0 && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
          <div className="flex min-w-0 items-start gap-2">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            <div>
              <p className="text-sm font-semibold">
                {archivedBlockerCount > 0
                  ? 'Start is blocked by archived or unfinished dependencies'
                  : 'Start is blocked by unfinished dependencies'}
              </p>
              <p className="mt-0.5 text-xs leading-5">
                {archivedBlockerCount > 0 ? (
                  <>
                    {archivedBlockerCount} archived prerequisite{archivedBlockerCount === 1 ? '' : 's'} must be restored or removed.
                    {unfinishedBlockerCount > 0 && (
                      <> {unfinishedBlockerCount} other blocker{unfinishedBlockerCount === 1 ? '' : 's'} still need attention.</>
                    )}
                  </>
                ) : (
                  <>{effectiveReadiness.blocking_count} prerequisite{effectiveReadiness.blocking_count === 1 ? '' : 's'} must reach Done.</>
                )}{' '}
                This gate applies before implementation and test cards start.
                {effectiveReadiness.blockers_truncated && (
                  <> The rows below show only a bounded sample of blockers.</>
                )}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              setDirection('depends_on');
              setFilters((current) => ({
                ...current,
                depends_on: { ...current.depends_on, satisfaction: 'unmet', activeState: 'active' },
              }));
            }}
            className="rounded-md border border-amber-300 px-2.5 py-1.5 text-xs font-semibold hover:bg-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 dark:border-amber-800 dark:hover:bg-amber-900/30"
          >
            Review blockers
          </button>
        </div>
      )}

      <AccessibleTabList
        idBase={`spec-${spec.id}-dependency-direction`}
        ariaLabel="Dependency direction"
        variant="secondary"
        items={[
          { id: 'depends_on', label: 'Depends on', icon: <Link2 size={13} />, count: effectiveReadiness?.active_dependency_count },
          { id: 'required_by', label: 'Required by', icon: <GitBranch size={13} /> },
        ]}
        value={direction}
        onValueChange={setDirection}
      />

      <AccessibleTabPanel
        idBase={`spec-${spec.id}-dependency-direction`}
        tabId={direction}
        value={direction}
        className="space-y-4"
      >
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5" aria-label={`${direction === 'depends_on' ? 'Depends on' : 'Required by'} filters`}>
        <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Satisfaction
          <select value={activeFilters.satisfaction} onChange={(event) => setFilter('satisfaction', event.target.value as SpecDependencySatisfactionFilter)} className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100">
            <option value="all">All states</option>
            <option value="unmet">Unfinished</option>
            <option value="satisfied">Satisfied</option>
          </select>
        </label>
        <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Lifecycle
          <select value={activeFilters.activeState} onChange={(event) => setFilter('activeState', event.target.value as SpecDependencyActiveStateFilter)} className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100">
            <option value="active">Active only</option>
            <option value="removed">Removed only</option>
            <option value="all">Active and removed</option>
          </select>
        </label>
        <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Introduction
          <select value={activeFilters.retrospective} onChange={(event) => setFilter('retrospective', event.target.value as DirectionFilters['retrospective'])} className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100">
            <option value="all">All introductions</option>
            <option value="planned">Added before start</option>
            <option value="retrospective">Retrospective</option>
          </select>
        </label>
        <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Lineage
          <select value={activeFilters.lineage} onChange={(event) => setFilter('lineage', event.target.value as SpecDependencyLineageFilter)} className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100">
            <option value="all">All ideations</option>
            <option value="same_ideation">Same ideation</option>
            <option value="cross_ideation">Cross-ideation</option>
          </select>
        </label>
        <label className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
          Related Spec status
          <select value={activeFilters.targetStatus} onChange={(event) => setFilter('targetStatus', event.target.value as SpecStatus | '')} className="mt-1 w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100">
            <option value="">All statuses</option>
            {SPEC_STATUSES.map((status) => <option key={status} value={status}>{SPEC_STATUS_LABELS[status]}</option>)}
          </select>
        </label>
      </div>

      {pager.loading && !pager.loaded && (
        <div role="status" className="flex items-center justify-center gap-2 py-12 text-sm text-gray-500 dark:text-gray-400">
          <Loader2 size={16} className="animate-spin" /> Loading dependencies…
        </div>
      )}
      {pager.error && (
        <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
          <p>{pager.error}</p>
          <button type="button" onClick={pager.retry} className="mt-2 inline-flex items-center gap-1 font-semibold hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500">
            <RefreshCw size={12} /> {pager.restartRequired ? 'Restart list' : 'Retry'}
          </button>
        </div>
      )}
      {!pager.loading && !pager.error && pager.loaded && pager.items.length === 0 && (
        <div className="rounded-lg border border-dashed border-gray-300 px-4 py-10 text-center dark:border-gray-700">
          <GitBranch size={24} className="mx-auto text-gray-300 dark:text-gray-600" />
          <p className="mt-2 text-sm font-medium text-gray-700 dark:text-gray-300">
            No {direction === 'depends_on' ? 'prerequisites' : 'dependent Specs'} match these filters.
          </p>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {direction === 'depends_on'
              ? 'Add a dependency or adjust the filters.'
              : 'Incoming dependency relationships appear here automatically.'}
          </p>
        </div>
      )}
      {!pager.error && pager.items.length > 0 && (
        <div className="space-y-3" aria-busy={pager.loading}>
          {pager.items.map((item) => (
            <DependencyRow
              key={item.id}
              item={item}
              canRemove={canRemove && dependencyReadCurrent}
              onOpenSpec={onOpenSpec}
              onRemove={(dependency, related) => setRemoving({ item: dependency, related })}
            />
          ))}
        </div>
      )}

      {pager.loaded && !pager.error && (
        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 pt-3 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
          <span>{total} result{total === 1 ? '' : 's'} · Page {pager.pageNumber} · Ordered by creation time and ID</span>
          <div className="flex gap-2">
            <button type="button" onClick={pager.previous} disabled={!pager.hasPrevious || pager.loading} className="btn btn-secondary text-xs disabled:opacity-50">Previous</button>
            <button type="button" onClick={pager.next} disabled={!pager.hasNext || pager.loading} className="btn btn-secondary text-xs disabled:opacity-50">Next</button>
          </div>
        </footer>
      )}
      </AccessibleTabPanel>

      {addOpen && (
        <AddDependencyDialog
          boardId={boardId}
          sourceSpecId={spec.id}
          sourceSpecVersion={spec.version}
          sourceSpecEdition={spec.edition}
          sourceStatus={spec.status}
          currentEditionStarted={effectiveReadiness?.current_edition_started ?? false}
          onClose={() => setAddOpen(false)}
          onAdded={refreshAfterMutation}
          readCurrent={dependencyReadCurrent}
          refreshKey={refreshKey}
          onAccessLost={loseAccess}
        />
      )}
      {removing && (
        <RemoveDependencyDialog
          boardId={boardId}
          sourceSpecId={spec.id}
          sourceSpecVersion={spec.version}
          sourceSpecEdition={spec.edition}
          item={removing.item}
          related={removing.related}
          onClose={() => setRemoving(null)}
          onRemoved={refreshAfterMutation}
          readCurrent={dependencyReadCurrent}
          onAccessLost={loseAccess}
          onSuccessfulClose={() => {
            window.setTimeout(() => workspaceHeadingRef.current?.focus(), 0);
          }}
        />
      )}
    </div>
  );
}

export default SpecDependenciesTab;
