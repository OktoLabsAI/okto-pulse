import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Archive,
  ArrowDown,
  ArrowUp,
  CalendarDays,
  Check,
  CircleGauge,
  FileText,
  FlaskConical,
  History,
  Lightbulb,
  Plus,
  Search,
  SquareKanban,
  Trash2,
  X,
} from 'lucide-react';

import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { ContextualHelpLink } from '@/components/help';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import type { Guideline } from '@/types';
import type {
  GuidelineRevisionDetail,
  GuidelineLifecycleStatus,
  GuidelineVersionBump,
  PolicyEntityType,
  RetireGuidelineRequest,
} from '@/types/policy-governance';

import {
  SYSTEM_CONFIDENCE_METRIC,
  canonicalSemanticMetrics,
  newSemanticMetricDraft,
  semanticMetricDraftToInput,
  semanticMetricToDraft,
  suggestMetricCode,
  validateSemanticMetricDraft,
  validateSemanticMetricDrafts,
  type SemanticMetricDraft,
} from './semanticMetricEditorModel';

import {
  GUIDELINE_ENTITY_TYPES,
  createGuidelineClientId,
} from './guidelineEditorShared';

const REVISION_PAGE_SIZE = 10;

export interface AdoptedGuidelineRevision {
  semanticVersion: string;
  revisionId?: string;
  bindingRevision?: number;
}

export interface GuidelineSuccessorOption {
  guidelineId: string;
  title: string;
  semanticVersion: string;
}

export interface GuidelineRevisionEditorProps {
  boardId: string;
  guideline: Guideline;
  adoptedRevision?: AdoptedGuidelineRevision;
  successorOptions?: GuidelineSuccessorOption[];
  initialSection?: 'metrics';
  onClose: () => void;
  onChanged: () => void | Promise<void>;
}

function errorMessage(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) {
    const minimum = error.details.minimum_bump;
    if (error.kind === 'under_bump' && minimum) {
      return `The selected version bump is too low. Minimum required: ${minimum}.`;
    }
    if (error.nextAction) return `${error.message} Next: ${error.nextAction}.`;
    return error.message;
  }
  return error instanceof Error ? error.message : 'Unexpected policy error.';
}

function bumpLabel(bump: GuidelineVersionBump): string {
  return `${bump.charAt(0).toUpperCase()}${bump.slice(1)} bump`;
}

const POLICY_TARGET_LABELS: Readonly<Record<PolicyEntityType, string>> = {
  ideation: 'Ideation',
  refinement: 'Refinement',
  spec: 'Spec',
  sprint: 'Sprint',
  card: 'Card',
  test_scenario: 'Test scenario',
};

function PolicyTargetIcon({
  target,
  size = 18,
}: {
  target: PolicyEntityType;
  size?: number;
}) {
  if (target === 'ideation') return <Lightbulb size={size} />;
  if (target === 'refinement') return <Search size={size} />;
  if (target === 'spec') return <FileText size={size} />;
  if (target === 'sprint') return <CalendarDays size={size} />;
  if (target === 'test_scenario') return <FlaskConical size={size} />;
  return <SquareKanban size={size} />;
}

function SystemConfidenceCard() {
  return (
    <article
      className="rounded-xl border border-violet-300 bg-gradient-to-br from-violet-50 to-white p-4 dark:border-violet-500/40 dark:from-violet-500/15 dark:to-gray-900"
      data-testid="system-confidence-metric"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-violet-600 text-white shadow-sm">
            <CircleGauge size={22} aria-hidden="true" />
          </span>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="text-base font-semibold text-gray-900 dark:text-white">
                {SYSTEM_CONFIDENCE_METRIC.title}
              </h4>
              <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-violet-700 dark:bg-violet-400/15 dark:text-violet-200">
                System-owned
              </span>
            </div>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-gray-600 dark:text-gray-300">
              {SYSTEM_CONFIDENCE_METRIC.description}
            </p>
          </div>
        </div>
        <span className="rounded-lg border border-violet-200 bg-white px-3 py-2 text-center dark:border-violet-500/30 dark:bg-gray-900">
          <span className="block text-[10px] font-semibold uppercase text-gray-500">
            Score
          </span>
          <span className="text-sm font-semibold text-gray-900 dark:text-white">
            0–100
          </span>
        </span>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_220px]">
        <div className="rounded-lg border border-violet-200/80 bg-white/80 p-3 dark:border-violet-500/25 dark:bg-gray-900/70">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-violet-700 dark:text-violet-200">
            Evaluation rubric
          </div>
          <p className="mt-1 text-xs leading-relaxed text-gray-600 dark:text-gray-300">
            {SYSTEM_CONFIDENCE_METRIC.evaluationRubric}
          </p>
        </div>
        <div className="rounded-lg border border-violet-200/80 bg-white/80 p-3 dark:border-violet-500/25 dark:bg-gray-900/70">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-violet-700 dark:text-violet-200">
            Board threshold
          </div>
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
            Configured when this guideline is added to a board. It is not part
            of the editable custom metric list.
          </p>
        </div>
      </div>
    </article>
  );
}

function SemanticMetricCard({
  metric,
  index,
  disabled,
  onChange,
  onRemove,
}: {
  metric: SemanticMetricDraft;
  index: number;
  disabled: boolean;
  onChange: (next: SemanticMetricDraft) => void;
  onRemove: () => void;
}) {
  const validationError = validateSemanticMetricDraft(metric);
  const toggleTarget = (target: PolicyEntityType) => {
    onChange({
      ...metric,
      targetEntityTypes: metric.targetEntityTypes.includes(target)
        ? metric.targetEntityTypes.filter((item) => item !== target)
        : [...metric.targetEntityTypes, target],
    });
  };

  return (
    <article
      className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-900"
      data-testid={`semantic-metric-editor-${index}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-gray-900 dark:text-white">
            Custom metric {index + 1}
          </div>
          <p className="mt-0.5 text-[11px] text-gray-500">
            Describe one semantic quality that an evaluator can score from 0
            to 100.
          </p>
        </div>
        <button
          type="button"
          aria-label={`Remove custom metric ${index + 1}`}
          disabled={disabled}
          onClick={onRemove}
          className="rounded-lg p-2 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-30 dark:hover:bg-red-500/10"
        >
          <Trash2 size={15} aria-hidden="true" />
        </button>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
          Metric title
          <input
            value={metric.title}
            disabled={disabled}
            onChange={(event) => {
              const title = event.target.value;
              onChange({
                ...metric,
                title,
                code: metric.originalCode === null
                  ? suggestMetricCode(title)
                  : metric.code,
              });
            }}
            placeholder="e.g. User value clarity"
            className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
          />
        </label>
        <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
          Description
          <input
            value={metric.description}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...metric, description: event.target.value })
            }
            placeholder="What this score communicates"
            className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
          />
        </label>
      </div>

      <label className="mt-4 block text-xs font-medium text-gray-700 dark:text-gray-200">
        Evaluation rubric
        <textarea
          aria-label="Evaluation rubric"
          value={metric.evaluationRubric}
          disabled={disabled}
          onChange={(event) =>
            onChange({ ...metric, evaluationRubric: event.target.value })
          }
          rows={4}
          placeholder="Explain what low, medium, and high scores mean. Include the evidence an evaluator should consider."
          className="mt-1 w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm leading-relaxed text-gray-900 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
        />
        <span className="mt-1 block text-[11px] font-normal text-gray-500">
          A concrete rubric makes human and AI evaluations more consistent and
          traceable.
        </span>
      </label>

      <fieldset className="mt-4">
        <legend className="text-xs font-semibold text-gray-700 dark:text-gray-200">
          Evaluated entities
        </legend>
        <p className="mb-2 mt-0.5 text-[11px] text-gray-500">
          Select every entity type for which this metric is meaningful.
        </p>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {GUIDELINE_ENTITY_TYPES.map((target) => {
            const selected = metric.targetEntityTypes.includes(target);
            return (
              <button
                key={target}
                type="button"
                aria-pressed={selected}
                aria-label={`${selected ? 'Remove' : 'Add'} ${POLICY_TARGET_LABELS[target]} metric target`}
                disabled={disabled}
                onClick={() => toggleTarget(target)}
                className={`group flex min-h-20 items-center gap-3 rounded-lg border p-3 text-left transition ${
                  selected
                    ? 'border-blue-500 bg-blue-50 text-blue-900 ring-1 ring-blue-500 dark:border-blue-400 dark:bg-blue-500/15 dark:text-blue-100'
                    : 'border-gray-200 bg-white text-gray-700 hover:border-blue-300 hover:bg-blue-50/50 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-200 dark:hover:border-blue-500/60 dark:hover:bg-blue-500/10'
                } disabled:cursor-not-allowed disabled:opacity-40`}
              >
                <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${
                  selected
                    ? 'bg-blue-600 text-white dark:bg-blue-500'
                    : 'bg-gray-100 text-gray-500 group-hover:text-blue-600 dark:bg-gray-800 dark:text-gray-300'
                }`}>
                  <PolicyTargetIcon target={target} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold">
                    {POLICY_TARGET_LABELS[target]}
                  </span>
                  <span className="mt-0.5 block text-[11px] opacity-70">
                    Score this entity
                  </span>
                </span>
                {selected && <Check size={16} aria-hidden="true" />}
              </button>
            );
          })}
        </div>
      </fieldset>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_260px]">
        <fieldset>
          <legend className="text-xs font-semibold text-gray-700 dark:text-gray-200">
            Passing direction
          </legend>
          <div className="mt-2 grid grid-cols-2 gap-2">
            {([
              {
                value: 'minimum' as const,
                label: 'Higher is better',
                description: 'Pass at or above the threshold',
                icon: ArrowUp,
              },
              {
                value: 'maximum' as const,
                label: 'Lower is better',
                description: 'Pass at or below the threshold',
                icon: ArrowDown,
              },
            ]).map((option) => {
              const selected = metric.direction === option.value;
              const Icon = option.icon;
              return (
                <button
                  key={option.value}
                  type="button"
                  aria-pressed={selected}
                  disabled={disabled}
                  onClick={() => onChange({
                    ...metric,
                    direction: option.value,
                  })}
                  className={`flex items-center gap-3 rounded-lg border p-3 text-left ${
                    selected
                      ? 'border-violet-500 bg-violet-50 ring-1 ring-violet-500 dark:border-violet-400 dark:bg-violet-500/15'
                      : 'border-gray-200 hover:border-violet-300 dark:border-gray-700 dark:hover:border-violet-500/60'
                  } disabled:opacity-40`}
                >
                  <Icon
                    size={18}
                    className={selected
                      ? 'text-violet-700 dark:text-violet-200'
                      : 'text-gray-400'}
                    aria-hidden="true"
                  />
                  <span>
                    <span className="block text-xs font-semibold text-gray-900 dark:text-white">
                      {option.label}
                    </span>
                    <span className="mt-0.5 block text-[10px] text-gray-500">
                      {option.description}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </fieldset>

        <label className="text-xs font-semibold text-gray-700 dark:text-gray-200">
          Default passing threshold
          <div className="mt-2 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-950/60">
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={0}
                max={100}
                step={1}
                value={metric.defaultThreshold}
                disabled={disabled}
                onChange={(event) => onChange({
                  ...metric,
                  defaultThreshold: event.target.value,
                })}
                aria-label={`Custom metric ${index + 1} default threshold`}
                className="min-w-0 flex-1 accent-violet-600"
              />
              <input
                type="number"
                aria-label={`Custom metric ${index + 1} default threshold value`}
                min={0}
                max={100}
                step={1}
                value={metric.defaultThreshold}
                disabled={disabled}
                onChange={(event) => onChange({
                  ...metric,
                  defaultThreshold: event.target.value,
                })}
                className="w-20 rounded-md border border-gray-300 bg-white px-2 py-1.5 text-center text-sm font-semibold text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
              />
            </div>
            <p className="mt-2 text-[10px] font-normal text-gray-500">
              Boards can override this value when adopting the guideline.
            </p>
          </div>
        </label>
      </div>

      <details className="mt-4 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-950/50">
        <summary className="cursor-pointer text-xs font-semibold text-gray-700 dark:text-gray-200">
          Technical details
        </summary>
        <dl className="mt-3 grid gap-3 md:grid-cols-2">
          <div>
            <dt className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
              Metric ID
            </dt>
            <dd className="mt-1 break-all font-mono text-xs text-gray-700 dark:text-gray-200">
              {metric.metricId}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
              Stable metric key
            </dt>
            <dd className="mt-1 break-all font-mono text-xs text-gray-700 dark:text-gray-200">
              {metric.code || 'Generated from the title'}
            </dd>
          </div>
        </dl>
      </details>

      {validationError && (
        <p
          role="alert"
          className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
        >
          {validationError}
        </p>
      )}
    </article>
  );
}

function RetirementDialog({
  guidelineTitle,
  successorOptions,
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  guidelineTitle: string;
  successorOptions: GuidelineSuccessorOption[];
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: (request: RetireGuidelineRequest) => void;
}) {
  const [status, setStatus] = useState<'retired' | 'superseded'>('retired');
  const [reason, setReason] = useState('');
  const [successor, setSuccessor] = useState('');
  const commandIdentityRef = useRef({
    signature: '',
    retirementId: createGuidelineClientId('retirement'),
    idempotencyKey: createGuidelineClientId('retirement-command'),
  });
  const focusTrap = useDialogFocusTrap(true, '[data-dialog-initial-focus]');
  useEscapeToClose(onCancel, {
    enabled: true,
    canClose: !busy,
    priority: 30,
  });

  const valid =
    reason.trim().length > 0
    && (status === 'retired' || successor.trim().length > 0);

  const confirm = () => {
    if (!valid || busy) return;
    const signature = JSON.stringify({
      status,
      reason: reason.trim(),
      successor: status === 'superseded' ? successor : null,
    });
    if (commandIdentityRef.current.signature !== signature) {
      commandIdentityRef.current = {
        signature,
        retirementId: createGuidelineClientId('retirement'),
        idempotencyKey: createGuidelineClientId('retirement-command'),
      };
    }
    const identity = commandIdentityRef.current;
    if (status === 'superseded') {
      onConfirm({
        retirement_id: identity.retirementId,
        idempotency_key: identity.idempotencyKey,
        status: 'superseded',
        reason: reason.trim(),
        superseded_by_guideline_id: successor,
      });
      return;
    }
    onConfirm({
      retirement_id: identity.retirementId,
      idempotency_key: identity.idempotencyKey,
      status: 'retired',
      reason: reason.trim(),
    });
  };

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/55 p-4">
      <div
        ref={focusTrap.dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="policy-retirement-title"
        aria-describedby="policy-retirement-description"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="w-full max-w-lg rounded-xl bg-white shadow-2xl dark:bg-gray-900"
      >
        <header className="border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <h3
            id="policy-retirement-title"
            className="flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-white"
          >
            <Archive size={18} className="text-amber-500" />
            Retire guideline
          </h3>
          <p
            id="policy-retirement-description"
            className="mt-1 text-sm text-gray-500"
          >
            {guidelineTitle} will remain resolvable in immutable history. This
            is not a hard delete.
          </p>
        </header>
        <div className="space-y-4 px-5 py-4">
          <fieldset>
            <legend className="text-xs font-semibold text-gray-700 dark:text-gray-200">
              Lifecycle outcome
            </legend>
            <div className="mt-2 flex gap-4">
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  data-dialog-initial-focus
                  checked={status === 'retired'}
                  disabled={busy}
                  onChange={() => setStatus('retired')}
                />
                Retired
              </label>
              <label className="inline-flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  checked={status === 'superseded'}
                  disabled={busy || successorOptions.length === 0}
                  onChange={() => setStatus('superseded')}
                />
                Superseded
              </label>
            </div>
          </fieldset>
          {status === 'superseded' && (
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-200">
              Successor guideline
              <select
                value={successor}
                disabled={busy}
                onChange={(event) => setSuccessor(event.target.value)}
                className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-xs dark:border-gray-700 dark:bg-gray-950"
              >
                <option value="">Select an active global guideline</option>
                {successorOptions.map((option) => (
                  <option key={option.guidelineId} value={option.guidelineId}>
                    {option.title} · v{option.semanticVersion}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="block text-xs font-medium text-gray-700 dark:text-gray-200">
            Reason
            <textarea
              value={reason}
              disabled={busy}
              onChange={(event) => setReason(event.target.value)}
              rows={4}
              placeholder="Why should this guideline stop accepting new adoptions?"
              className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-700 dark:bg-gray-950"
            />
          </label>
          {error && (
            <div
              role="alert"
              className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200"
            >
              {error}
            </div>
          )}
        </div>
        <footer className="flex justify-end gap-2 border-t border-gray-200 px-5 py-4 dark:border-gray-700">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="btn btn-secondary"
          >
            Keep active
          </button>
          <button
            type="button"
            disabled={!valid || busy}
            onClick={confirm}
            className="btn btn-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? 'Retiring…' : 'Confirm retirement'}
          </button>
        </footer>
      </div>
    </div>
  );
}

export function GuidelineRevisionEditor({
  boardId,
  guideline,
  adoptedRevision,
  successorOptions = [],
  initialSection,
  onClose,
  onChanged,
}: GuidelineRevisionEditorProps) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const canRead =
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
    && permissions.has('guidelines.revisions.read');
  const canCreateRevision =
    canRead && permissions.has('guidelines.revisions.create');
  const canAuthorMetrics =
    canCreateRevision && permissions.has('guidelines.metrics.author');
  const canRetire =
    canRead && permissions.has('guidelines.revisions.retire');
  const authorityError = permissions.error
    ? 'Permission status is unavailable. Policy mutations fail closed.'
    : permissions.ownerReviewRequired
      ? 'Owner review is required before policy actions are available.'
      : !permissions.isLoading && !canRead
        ? 'You do not have permission to read guideline revisions.'
        : null;
  const focusTrap = useDialogFocusTrap(
    true,
    '[data-dialog-initial-focus]',
  );
  const [revisions, setRevisions] = useState<GuidelineRevisionDetail[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [initialLoadError, setInitialLoadError] = useState<string | null>(null);
  const [paginationError, setPaginationError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationResult, setMutationResult] = useState<string | null>(null);
  const [retirementStatus, setRetirementStatus] =
    useState<GuidelineLifecycleStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [retirementOpen, setRetirementOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [metrics, setMetrics] = useState<SemanticMetricDraft[]>([]);
  const [versionBump, setVersionBump] =
    useState<GuidelineVersionBump>('patch');
  const [currentHeadRevision, setCurrentHeadRevision] =
    useState<number | null>(null);
  const metricsSectionRef = useRef<HTMLElement | null>(null);
  const initialSectionAppliedRef = useRef(false);
  const seenHistoryCursorsRef = useRef(new Set<string>());

  useEscapeToClose(onClose, {
    enabled: !retirementOpen,
    canClose: !saving,
    priority: 20,
  });

  const latest = revisions[0];
  const busy =
    permissions.isLoading || loading || loadingMore || saving;

  const resetDraft = useCallback((revision: GuidelineRevisionDetail) => {
    setTitle(revision.title);
    setContent(revision.content);
    setMetrics(revision.metrics.map(semanticMetricToDraft));
    setVersionBump('patch');
    setMutationError(null);
  }, []);

  const loadFirstPage = useCallback(async () => {
    if (!canRead) {
      setLoading(false);
      setInitialLoadError(authorityError);
      setRevisions([]);
      setRetirementStatus(null);
      setCurrentHeadRevision(null);
      setNextCursor(null);
      return;
    }
    setLoading(true);
    setInitialLoadError(null);
    setPaginationError(null);
    seenHistoryCursorsRef.current.clear();
    try {
      const page = await api.listGuidelineRevisions(
        boardId,
        guideline.id,
        { limit: REVISION_PAGE_SIZE, projection: 'detail' },
      );
      const detailItems = page.items.filter(
        (item): item is GuidelineRevisionDetail =>
          item.projection === 'detail',
      );
      if (detailItems.length === 0) {
        throw new Error('No immutable revisions were returned.');
      }
      const authority = await api.getGuidelineRevision(
        boardId,
        guideline.id,
        detailItems[0].revision_id,
      );
      if (
        authority.head.revision_id !== detailItems[0].revision_id
        || authority.revision.revision_id !== detailItems[0].revision_id
      ) {
        throw new Error('Authoritative guideline head changed while loading.');
      }
      setRevisions(detailItems);
      setRetirementStatus(authority.retirement?.status ?? null);
      setCurrentHeadRevision(authority.head.head_revision);
      setNextCursor(page.has_more ? page.next_cursor : null);
      resetDraft(detailItems[0]);
    } catch (error) {
      setInitialLoadError(errorMessage(error));
      setRevisions([]);
      setRetirementStatus(null);
      setCurrentHeadRevision(null);
      setNextCursor(null);
    } finally {
      setLoading(false);
    }
  }, [
    api,
    authorityError,
    boardId,
    canRead,
    guideline.id,
    resetDraft,
  ]);

  useEffect(() => {
    if (permissions.isLoading) return;
    void loadFirstPage();
  }, [loadFirstPage, permissions.isLoading]);

  useEffect(() => {
    if (
      initialSection !== 'metrics'
      || initialSectionAppliedRef.current
      || loading
      || !latest
      || !metricsSectionRef.current
    ) {
      return undefined;
    }
    initialSectionAppliedRef.current = true;
    const focusMetrics = () => {
      metricsSectionRef.current?.scrollIntoView?.({
        behavior: 'smooth',
        block: 'start',
      });
      metricsSectionRef.current?.focus({ preventScroll: true });
    };
    if (typeof requestAnimationFrame === 'function') {
      const frame = requestAnimationFrame(focusMetrics);
      return () => cancelAnimationFrame(frame);
    }
    const timeout = window.setTimeout(focusMetrics, 0);
    return () => window.clearTimeout(timeout);
  }, [initialSection, latest, loading]);

  const draftMetricInputs = useMemo(() => {
    try {
      return metrics.map(semanticMetricDraftToInput);
    } catch {
      return null;
    }
  }, [metrics]);

  const currentMetricInputs = useMemo(
    () => latest?.metrics ?? [],
    [latest],
  );

  const metricsChanged = useMemo(
    () => Boolean(
      draftMetricInputs
      && JSON.stringify(canonicalSemanticMetrics(draftMetricInputs))
        !== JSON.stringify(canonicalSemanticMetrics(currentMetricInputs)),
    ),
    [currentMetricInputs, draftMetricInputs],
  );

  const changeSummary = useMemo(() => {
    if (!latest) return [];
    const changes: string[] = [];
    if (title.trim() !== latest.title) changes.push('title');
    if (content.trim() !== latest.content) changes.push('body');
    if (metricsChanged) changes.push('semantic metrics');
    return changes;
  }, [
    content,
    latest,
    metricsChanged,
    title,
  ]);

  const metricError = useMemo(
    () => validateSemanticMetricDrafts(metrics),
    [metrics],
  );

  const saveRevision = async () => {
    if (
      !latest
      || currentHeadRevision === null
      || !draftMetricInputs
      || metricError
      || !canCreateRevision
      || (metricsChanged && !canAuthorMetrics)
      || !title.trim()
      || !content.trim()
      || changeSummary.length === 0
    ) {
      return;
    }

    setSaving(true);
    setMutationError(null);
    setMutationResult(null);
    try {
      const response = await api.createGuidelineRevision(
        boardId,
        guideline.id,
        {
          expected_head_revision: currentHeadRevision,
          version_bump: versionBump,
          content: {
            title: title.trim(),
            body: content.trim(),
          },
          metrics: draftMetricInputs,
        },
      );
      setMutationResult(
        `Created v${response.revision} · ${bumpLabel(versionBump)}.`,
      );
      await loadFirstPage();
      await onChanged();
    } catch (error) {
      setMutationError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const loadOlder = async () => {
    if (!nextCursor || loadingMore) return;
    const requestCursor = nextCursor;
    if (seenHistoryCursorsRef.current.has(requestCursor)) {
      setPaginationError(
        'The revision cursor repeated. Restart history before continuing.',
      );
      setNextCursor(null);
      return;
    }
    seenHistoryCursorsRef.current.add(requestCursor);
    setLoadingMore(true);
    setPaginationError(null);
    try {
      const page = await api.listGuidelineRevisions(
        boardId,
        guideline.id,
        {
          limit: REVISION_PAGE_SIZE,
          projection: 'detail',
          cursor: requestCursor,
        },
      );
      const detailItems = page.items.filter(
          (item): item is GuidelineRevisionDetail =>
            item.projection === 'detail',
        );
      setRevisions((current) => {
        const existing = new Set(current.map((item) => item.revision_id));
        return [
          ...current,
          ...detailItems.filter((item) => !existing.has(item.revision_id)),
        ];
      });
      if (
        page.has_more
        && (
          page.next_cursor === requestCursor
          || seenHistoryCursorsRef.current.has(page.next_cursor)
        )
      ) {
        setNextCursor(null);
        setPaginationError(
          'The server returned a repeated cursor. Restart history before continuing.',
        );
      } else {
        setNextCursor(page.has_more ? page.next_cursor : null);
      }
    } catch (error) {
      if (
        error instanceof PolicyGovernanceApiError
        && error.kind === 'invalid_cursor'
      ) {
        setNextCursor(null);
        setPaginationError(
          'This history cursor expired or no longer matches the projection. Restart history.',
        );
      } else {
        setPaginationError(errorMessage(error));
      }
    } finally {
      setLoadingMore(false);
    }
  };

  const retire = async (request: RetireGuidelineRequest) => {
    if (!canRetire) return;
    setSaving(true);
    setMutationError(null);
    try {
      await api.retireGuideline(boardId, guideline.id, request);
      setRetirementOpen(false);
      await onChanged();
      onClose();
    } catch (error) {
      setMutationError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const metricTargets = latest
    ? Array.from(
        new Set(latest.metrics.flatMap((metric) => metric.target_entity_types)),
      )
    : [];
  const revisionUpdateAvailable = Boolean(
    latest
    && adoptedRevision
    && (
      adoptedRevision.revisionId
        ? latest.revision_id !== adoptedRevision.revisionId
        : latest.semantic_version !== adoptedRevision.semanticVersion
    ),
  );

  const updateMetric = (index: number, next: SemanticMetricDraft) => {
    setMetrics((current) =>
      current.map((metric, currentIndex) =>
        currentIndex === index ? next : metric,
      ),
    );
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/55 p-4">
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="guideline-revision-editor-title"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="flex h-[94vh] w-full max-w-7xl flex-col overflow-hidden rounded-xl border border-gray-200 bg-gray-50 shadow-2xl dark:border-gray-700 dark:bg-gray-950"
      >
        <header className="flex items-start justify-between gap-4 border-b border-gray-200 bg-white px-6 py-4 dark:border-gray-700 dark:bg-gray-900">
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-blue-600 dark:text-blue-300">
              Guideline revision
            </div>
            <h2
              id="guideline-revision-editor-title"
              className="mt-1 text-xl font-semibold text-gray-900 dark:text-white"
            >
              Edit guideline
            </h2>
            <p className="mt-1 text-sm text-gray-500">
              <span className="font-medium text-gray-700 dark:text-gray-200">
                {guideline.title}
              </span>
              {' '}· Saving creates a new version so previous content remains
              traceable.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <ContextualHelpLink
              sectionId="policy-governance"
              testId="guideline-revision-help"
            >
              Revision guide
            </ContextualHelpLink>
            <button
              type="button"
              data-dialog-initial-focus
              aria-label="Close revision editor"
              disabled={saving}
              onClick={onClose}
              className="rounded-lg p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40 dark:hover:bg-gray-800"
            >
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="grid min-h-0 flex-1 lg:grid-cols-[320px_minmax(0,1fr)]">
          <aside className="overflow-y-auto border-r border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900">
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <div className="text-[10px] font-semibold uppercase text-gray-500">
                  Adopted
                </div>
                <div className="mt-1 text-sm font-semibold text-gray-900 dark:text-white">
                  {adoptedRevision
                    ? `v${adoptedRevision.semanticVersion}`
                    : 'Not adopted'}
                </div>
                {adoptedRevision?.bindingRevision !== undefined && (
                  <div className="mt-0.5 text-[10px] text-gray-500">
                    Binding revision {adoptedRevision.bindingRevision}
                  </div>
                )}
              </div>
              <div className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <div className="text-[10px] font-semibold uppercase text-gray-500">
                  Latest
                </div>
                <div className="mt-1 text-sm font-semibold text-gray-900 dark:text-white">
                  {latest ? `v${latest.semantic_version}` : 'Unavailable'}
                </div>
                {revisionUpdateAvailable && (
                    <div className="mt-0.5 text-[10px] text-amber-600 dark:text-amber-300">
                      Update available
                    </div>
                  )}
              </div>
            </div>

            <div className="mt-4 rounded-lg border border-blue-200 bg-blue-50 p-3 dark:border-blue-500/30 dark:bg-blue-500/10">
              <div className="text-[10px] font-semibold uppercase text-blue-700 dark:text-blue-200">
                Context scope
              </div>
              <div className="mt-1 text-sm font-semibold text-blue-950 dark:text-blue-100">
                All entities
              </div>
              <p className="mt-1 text-xs text-blue-800/75 dark:text-blue-100/70">
                Context availability is independent from executable targets
                and enforcement.
              </p>
            </div>

            <div className="mt-4">
              <div className="text-xs font-semibold text-gray-700 dark:text-gray-200">
                Semantic metric targets
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {metricTargets.length > 0 ? (
                  metricTargets.map((target) => (
                    <span
                      key={target}
                      className="rounded bg-gray-100 px-2 py-1 text-[10px] text-gray-700 dark:bg-gray-800 dark:text-gray-200"
                    >
                      {target}
                    </span>
                  ))
                ) : (
                  <span className="text-xs text-gray-500">
                    Context only · no custom metrics
                  </span>
                )}
              </div>
            </div>

            <div className="mt-5 border-t border-gray-200 pt-4 dark:border-gray-700">
              <div className="flex items-center gap-2 text-xs font-semibold text-gray-700 dark:text-gray-200">
                <History size={14} />
                Revision history
              </div>
              <div className="mt-2 space-y-2" data-testid="guideline-revision-history">
                {revisions.map((revision, index) => (
                  <div
                    key={revision.revision_id}
                    className={`rounded-md border p-2.5 ${
                      index === 0
                        ? 'border-blue-300 bg-blue-50 dark:border-blue-500/40 dark:bg-blue-500/10'
                        : 'border-gray-200 dark:border-gray-700'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-gray-900 dark:text-white">
                        v{revision.semantic_version}
                      </span>
                      <span className="text-[10px] text-gray-500">
                        #{revision.revision_number}
                      </span>
                    </div>
                    <div className="mt-1 truncate text-[11px] text-gray-600 dark:text-gray-300">
                      {revision.title}
                    </div>
                    <div className="mt-1 text-[10px] text-gray-500">
                      {revision.metrics.length} custom metric
                      {revision.metrics.length === 1 ? '' : 's'}
                    </div>
                  </div>
                ))}
              </div>
              {nextCursor && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void loadOlder()}
                  className="mt-3 w-full rounded-md border border-gray-300 px-3 py-2 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  {loadingMore ? 'Loading…' : 'Load older revisions'}
                </button>
              )}
              {paginationError && (
                <div
                  role="alert"
                  className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
                >
                  <p>{paginationError}</p>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void loadFirstPage()}
                    className="mt-2 rounded border border-current px-2 py-1 font-semibold"
                  >
                    Restart history
                  </button>
                </div>
              )}
            </div>

            <button
              type="button"
              disabled={
                busy
                || Boolean(initialLoadError)
                || !canRetire
                || retirementStatus !== null
              }
              title={
                canRetire
                  ? retirementStatus
                    ? `Guideline is already ${retirementStatus}`
                    : 'Retire this guideline without deleting history'
                  : 'Requires guidelines.revisions.retire'
              }
              onClick={() => {
                setMutationError(null);
                setRetirementOpen(true);
              }}
              className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-md border border-amber-300 px-3 py-2 text-xs font-semibold text-amber-700 hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-amber-500/40 dark:text-amber-200 dark:hover:bg-amber-500/10"
            >
              <Archive size={14} />
              {retirementStatus
                ? `Guideline ${retirementStatus}`
                : 'Retire guideline'}
            </button>
          </aside>

          <main className="min-w-0 overflow-y-auto p-6">
            {loading && (
              <div className="py-16 text-center text-sm text-gray-500">
                Loading immutable revisions…
              </div>
            )}
            {initialLoadError && (
              <div
                role="alert"
                className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200"
              >
                <div className="font-semibold">Revision editor unavailable</div>
                <p className="mt-1">{initialLoadError}</p>
                <p className="mt-2 text-xs">
                  Mutation controls remain disabled until the authoritative
                  revision history loads successfully.
                </p>
                <button
                  type="button"
                  onClick={() => void loadFirstPage()}
                  className="mt-3 rounded border border-current px-3 py-1.5 text-xs font-semibold"
                >
                  Retry
                </button>
              </div>
            )}

            {!loading && !initialLoadError && latest && (
              <div className="space-y-5">
                {!canCreateRevision && (
                  <div
                    role="status"
                    className="rounded-lg border border-gray-200 bg-white p-3 text-sm text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
                  >
                    Revision history is read-only. Creating a revision requires
                    guidelines.revisions.create.
                  </div>
                )}
                <section>
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                        New revision
                      </h3>
                      <p className="mt-1 text-sm text-gray-500">
                        Choose the SemVer impact of this immutable revision.
                        The current head revision is fenced to prevent
                        overwriting concurrent edits.
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={busy || !canCreateRevision}
                      onClick={() => resetDraft(latest)}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-xs text-gray-600 hover:bg-white disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-900"
                    >
                      Reset draft
                    </button>
                  </div>

                  <div className="mt-4 grid gap-4 md:grid-cols-[minmax(0,1fr)_220px]">
                    <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
                      Title
                      <input
                        value={title}
                        disabled={busy || !canCreateRevision}
                        onChange={(event) => setTitle(event.target.value)}
                        className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
                      />
                    </label>
                    <label className="text-xs font-medium text-gray-700 dark:text-gray-200">
                      Version bump
                      <select
                        aria-label="Version bump"
                        value={versionBump}
                        disabled={busy || !canCreateRevision}
                        onChange={(event) =>
                          setVersionBump(
                            event.target.value as GuidelineVersionBump,
                          )
                        }
                        className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
                      >
                        <option value="patch">Patch · clarification</option>
                        <option value="minor">Minor · additive behavior</option>
                        <option value="major">Major · breaking meaning</option>
                      </select>
                      <span className="mt-1 block text-[10px] font-normal leading-relaxed text-gray-500">
                        Select minor for additive capabilities and major when
                        existing consumers must change.
                      </span>
                    </label>
                  </div>
                  <label className="mt-4 block text-xs font-medium text-gray-700 dark:text-gray-200">
                    Guideline body
                    <textarea
                      value={content}
                      disabled={busy || !canCreateRevision}
                      onChange={(event) => setContent(event.target.value)}
                      rows={8}
                      className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
                    />
                  </label>
                </section>

                <section
                  ref={metricsSectionRef}
                  tabIndex={-1}
                  aria-labelledby="guideline-semantic-metrics-title"
                  className="scroll-mt-6 focus:outline-none"
                >
                  {canCreateRevision && !canAuthorMetrics && (
                    <div
                      role="status"
                      data-testid="semantic-metrics-readonly"
                      className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200"
                    >
                      Semantic metrics are read-only. You may still create a
                      text-only revision that preserves the current metrics.
                      Changing metrics requires{' '}
                      <code>guidelines.metrics.author</code> and its historical
                      authority <code>spec.entity.edit_fields</code>.
                    </div>
                  )}
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h3
                        id="guideline-semantic-metrics-title"
                        className="text-base font-semibold text-gray-900 dark:text-white"
                      >
                        Semantic metrics
                      </h3>
                      <p className="mt-1 text-xs text-gray-500">
                        Define qualities that humans and agents score from 0 to
                        100. Board-level enforcement is configured during
                        adoption.
                      </p>
                      <ContextualHelpLink
                        sectionId="semantic-guideline-metrics"
                        className="mt-2 text-[11px]"
                        testId="semantic-metrics-help"
                      >
                        How semantic metrics work
                      </ContextualHelpLink>
                    </div>
                    <button
                      type="button"
                      data-testid="add-semantic-metric"
                      disabled={busy || !canAuthorMetrics}
                      title={
                        canAuthorMetrics
                          ? 'Add a custom semantic metric'
                          : 'Requires guidelines.metrics.author and spec.entity.edit_fields'
                      }
                      onClick={() => setMetrics((current) => [
                        ...current,
                        newSemanticMetricDraft(),
                      ])}
                      className="inline-flex items-center gap-1 rounded-md bg-blue-600 px-3 py-2 text-xs font-semibold text-white hover:bg-blue-700 disabled:opacity-40"
                    >
                      <Plus size={13} />
                      Add custom metric
                    </button>
                  </div>

                  <div className="mt-3 space-y-3">
                    <SystemConfidenceCard />
                    {metrics.length === 0 ? (
                      <div className="rounded-lg border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500 dark:border-gray-700">
                        <strong className="block text-gray-700 dark:text-gray-200">
                          Context-only guideline
                        </strong>
                        <span className="mt-1 block">
                          This revision has no custom semantic metrics.
                          Confidence remains system-owned assessment metadata
                          and is configured at board adoption.
                        </span>
                      </div>
                    ) : (
                      metrics.map((metric, index) => (
                        <SemanticMetricCard
                          key={metric.localId}
                          metric={metric}
                          index={index}
                          disabled={busy || !canAuthorMetrics}
                          onChange={(next) => updateMetric(index, next)}
                          onRemove={() =>
                            setMetrics((current) =>
                              current.filter(
                                (_, currentIndex) => currentIndex !== index,
                              ),
                            )
                          }
                        />
                      ))
                    )}
                  </div>
                </section>

                <section className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
                  <div className="text-xs font-semibold uppercase text-gray-500">
                    Change summary
                  </div>
                  {changeSummary.length > 0 ? (
                    <ul className="mt-2 flex flex-wrap gap-2">
                      {changeSummary.map((change) => (
                        <li
                          key={change}
                          className="rounded bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700 dark:bg-blue-500/15 dark:text-blue-200"
                        >
                          {change}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-2 text-sm text-gray-500">
                      No changes from v{latest.semantic_version}.
                    </p>
                  )}
                  {metricError && (
                    <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
                      {metricError}
                    </p>
                  )}
                  {mutationError && (
                    <div
                      role="alert"
                      className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200"
                    >
                      {mutationError}
                    </div>
                  )}
                  {mutationResult && (
                    <div
                      role="status"
                      className="mt-3 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-700 dark:border-green-500/30 dark:bg-green-500/10 dark:text-green-200"
                    >
                      {mutationResult}
                    </div>
                  )}
                  <div className="mt-4 flex justify-end">
                    <button
                      type="button"
                      data-testid="create-guideline-revision"
                      disabled={
                        busy
                        || !canCreateRevision
                        || (metricsChanged && !canAuthorMetrics)
                        || currentHeadRevision === null
                        || changeSummary.length === 0
                        || Boolean(metricError)
                        || !title.trim()
                        || !content.trim()
                      }
                      onClick={() => void saveRevision()}
                      className="btn btn-primary disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {saving ? 'Creating revision…' : 'Create immutable revision'}
                    </button>
                  </div>
                </section>
              </div>
            )}
          </main>
        </div>
      </div>

      {retirementOpen && (
        <RetirementDialog
          guidelineTitle={guideline.title}
          successorOptions={successorOptions}
          busy={saving}
          error={mutationError}
          onCancel={() => {
            if (!saving) {
              setMutationError(null);
              setRetirementOpen(false);
            }
          }}
          onConfirm={(request) => void retire(request)}
        />
      )}
    </div>
  );
}
