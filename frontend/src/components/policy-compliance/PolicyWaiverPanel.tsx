import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock3,
  FileWarning,
  Filter,
  History,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from 'lucide-react';

import { ContextualHelpLink } from '@/components/help';
import { CollapsibleEvidenceSection } from '@/components/shared/CollapsibleEvidenceSection';
import { CursorCollectionControls } from '@/components/shared/CursorCollectionControls';
import { useOpaqueCursorCollection } from '@/hooks/useOpaqueCursorCollection';
import { usePermissions } from '@/hooks/usePermissions';
import { usePolicyGovernanceApi } from '@/services/policy-governance-api';
import type {
  PolicyEntityType,
  PolicyWaiverStatus,
  SemanticEvidenceRef,
  SemanticWaiverEvent,
  SemanticWaiverFull,
} from '@/types/policy-governance';

import {
  formatPolicyTimestamp,
  formatPolicyToken,
} from './policyUiModel';
import {
  PolicyWaiverActionDialog,
  type PolicyWaiverAction,
  type PolicyWaiverMutationResult,
} from './PolicyWaiverDialogs';
import {
  POLICY_WAIVER_EVENT_LABEL,
  POLICY_WAIVER_STATUS_LABEL,
  classifyPolicyWaiverCursorError,
  parseSemanticWaiverHeadResponse,
  policyWaiverErrorMessage,
  policyWaiverExpireReasonLabel,
  validatedSemanticWaiverEvents,
  validatedSemanticWaiverPage,
} from './policyWaiverModel';

const WAIVER_PAGE_SIZE = 25;
const STATUS_OPTIONS: PolicyWaiverStatus[] = [
  'requested',
  'approved',
  'rejected',
  'revoked',
  'expired',
];
const ENTITY_TYPE_OPTIONS: PolicyEntityType[] = [
  'ideation',
  'refinement',
  'spec',
  'sprint',
  'card',
  'test_scenario',
];

const STATUS_TONE: Record<PolicyWaiverStatus, string> = {
  requested:
    'bg-amber-100 text-amber-800 dark:bg-amber-400/15 dark:text-amber-200',
  approved:
    'bg-emerald-100 text-emerald-800 dark:bg-emerald-400/15 dark:text-emerald-200',
  rejected:
    'bg-red-100 text-red-800 dark:bg-red-400/15 dark:text-red-200',
  revoked:
    'bg-surface-200 text-surface-700 dark:bg-surface-700 dark:text-surface-200',
  expired:
    'bg-orange-100 text-orange-800 dark:bg-orange-400/15 dark:text-orange-200',
};

interface SnapshotState {
  evaluatedAt: string;
  generation: number;
}

interface WaiverFilters {
  subjectId: string;
  metricResultId: string;
  findingId: string;
  receiptId: string;
}

interface WaiverActionSelection {
  waiver: SemanticWaiverFull;
  action: PolicyWaiverAction;
  evaluatedAt: string;
}

function newSnapshot(
  generation = 0,
  previousEvaluatedAt?: string,
): SnapshotState {
  const previous = previousEvaluatedAt
    ? Date.parse(previousEvaluatedAt)
    : Number.NEGATIVE_INFINITY;
  const now = Date.now();
  return {
    evaluatedAt: new Date(Math.max(now, previous + 1)).toISOString(),
    generation,
  };
}

function EvidenceReferences({
  references,
}: {
  references: readonly SemanticEvidenceRef[];
}) {
  return (
    <ul className="space-y-2">
      {references.map((reference) => (
        <li
          key={[
            reference.source_type,
            reference.source_id,
            reference.source_version,
            reference.content_hash,
          ].join(':')}
          className="rounded-lg border border-surface-200 bg-white p-2 text-[11px] dark:border-surface-700 dark:bg-surface-900/60"
        >
          <p className="font-semibold text-surface-700 dark:text-surface-200">
            {reference.source_type} · {reference.source_id} · v
            {reference.source_version}
          </p>
          <p className="mt-1 break-all font-mono text-[10px] text-surface-500">
            SHA-256 {reference.content_hash}
          </p>
        </li>
      ))}
    </ul>
  );
}

function WaiverHistory({
  boardId,
  evaluatedAt,
  waiver,
}: {
  boardId: string;
  evaluatedAt: string;
  waiver: SemanticWaiverFull;
}) {
  const api = usePolicyGovernanceApi();
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [head, setHead] = useState<SemanticWaiverFull | null>(null);
  const [events, setEvents] = useState<SemanticWaiverEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError(null);
    try {
      const detailResponse = await api.getSemanticMetricWaiver(
        boardId,
        waiver.waiver_id,
        {
          evaluatedAt,
          projection: 'full',
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      const currentHead = parseSemanticWaiverHeadResponse(detailResponse, {
        boardId,
        evaluatedAt,
        waiverId: waiver.waiver_id,
        findingId: waiver.finding_id,
        metricResultId: waiver.metric_result_id,
      });
      const eventResponse = await api.listSemanticMetricWaiverEvents(
        boardId,
        waiver.waiver_id,
        controller.signal,
      );
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      setHead(currentHead);
      setEvents(validatedSemanticWaiverEvents(eventResponse, currentHead));
      setLoaded(true);
    } catch (caught) {
      if (controller.signal.aborted || requestId !== requestRef.current) {
        return;
      }
      setHead(null);
      setEvents([]);
      setLoaded(true);
      setError(policyWaiverErrorMessage(caught));
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [
    api,
    boardId,
    evaluatedAt,
    waiver.finding_id,
    waiver.metric_result_id,
    waiver.waiver_id,
  ]);

  useEffect(() => () => controllerRef.current?.abort(), []);

  useEffect(() => {
    requestRef.current += 1;
    controllerRef.current?.abort();
    setExpanded(false);
    setLoading(false);
    setLoaded(false);
    setHead(null);
    setEvents([]);
    setError(null);
  }, [evaluatedAt, waiver.waiver_id, waiver.waiver_revision]);

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && !loaded && !loading) void load();
  };

  return (
    <CollapsibleEvidenceSection
      title="Immutable event history"
      description="Load and verify the authoritative head against its contiguous append-only chain."
      expanded={expanded}
      onToggle={toggle}
      testId={`policy-waiver-history-${waiver.waiver_id}`}
    >
      {loading && !loaded ? (
        <p role="status" className="text-xs text-surface-500">
          Loading semantic waiver history…
        </p>
      ) : error ? (
        <div className="space-y-2">
          <p role="alert" className="text-xs text-red-700 dark:text-red-300">
            Could not verify semantic waiver history. {error}
          </p>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="rounded-lg border border-red-300 px-2.5 py-1 text-xs font-semibold text-red-700 disabled:opacity-50 dark:border-red-700 dark:text-red-200"
          >
            Retry history
          </button>
        </div>
      ) : head ? (
        <>
          <p className="rounded-lg border border-surface-200 bg-surface-50 p-2 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-950/40 dark:text-surface-300">
            Verified head revision {head.waiver_revision}
            {' · '}
            {POLICY_WAIVER_EVENT_LABEL[head.last_event_type]}
            {' · '}
            {formatPolicyTimestamp(head.last_event_at)}
          </p>
          <ol className="space-y-2">
            {events.map((event) => (
              <li
                key={event.event_id}
                className="rounded-lg border border-surface-200 bg-white p-3 text-xs dark:border-surface-700 dark:bg-surface-900/60"
                data-testid={`policy-waiver-event-${event.event_id}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <strong className="text-surface-800 dark:text-surface-100">
                    r{event.waiver_revision}
                    {' · '}
                    {POLICY_WAIVER_EVENT_LABEL[event.event_type]}
                  </strong>
                  <span className="text-surface-500">
                    {formatPolicyTimestamp(event.occurred_at)}
                  </span>
                </div>
                <p className="mt-1 text-surface-600 dark:text-surface-300">
                  Actor {event.actor_id}
                  {' · '}
                  {event.from_status
                    ? `${POLICY_WAIVER_STATUS_LABEL[event.from_status]} → `
                    : ''}
                  {POLICY_WAIVER_STATUS_LABEL[event.to_status]}
                </p>
                <p className="mt-2 whitespace-pre-wrap text-surface-700 dark:text-surface-200">
                  {event.reason}
                </p>
                <div className="mt-2">
                  <EvidenceReferences references={event.evidence_refs} />
                </div>
                {event.event_type === 'revalidate' && (
                  <p className="mt-2 text-surface-600 dark:text-surface-300">
                    Decision {formatPolicyToken(
                      event.revalidation_status ?? 'unknown',
                    )}
                    {' · '}
                    {formatPolicyToken(
                      event.revalidation_reason_code ?? 'unknown',
                    )}
                    {' · '}
                    {event.revalidation_current ? 'current' : 'not current'}
                  </p>
                )}
              </li>
            ))}
          </ol>
        </>
      ) : null}
    </CollapsibleEvidenceSection>
  );
}

function availableActions({
  waiver,
  canReview,
  canRevoke,
  canRevalidate,
}: {
  waiver: SemanticWaiverFull;
  canReview: boolean;
  canRevoke: boolean;
  canRevalidate: boolean;
}): PolicyWaiverAction[] {
  switch (waiver.status) {
    case 'requested':
      return canReview ? ['approve', 'reject'] : [];
    case 'approved':
      return [
        ...(canRevoke ? ['revoke' as const] : []),
        ...(canRevalidate ? ['revalidate' as const] : []),
      ];
    case 'expired':
    case 'revoked':
      return canRevalidate ? ['revalidate'] : [];
    case 'rejected':
      return [];
  }
}

function ActionIcon({ action }: { action: PolicyWaiverAction }) {
  switch (action) {
    case 'approve':
      return <CheckCircle2 size={13} aria-hidden="true" />;
    case 'reject':
      return <XCircle size={13} aria-hidden="true" />;
    case 'revoke':
      return <Ban size={13} aria-hidden="true" />;
    case 'revalidate':
      return <RefreshCw size={13} aria-hidden="true" />;
  }
}

function WaiverRow({
  boardId,
  evaluatedAt,
  waiver,
  canReview,
  canRevoke,
  canRevalidate,
  onAction,
}: {
  boardId: string;
  evaluatedAt: string;
  waiver: SemanticWaiverFull;
  canReview: boolean;
  canRevoke: boolean;
  canRevalidate: boolean;
  onAction: (
    waiver: SemanticWaiverFull,
    action: PolicyWaiverAction,
  ) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const actions = availableActions({
    waiver,
    canReview,
    canRevoke,
    canRevalidate,
  });
  return (
    <li
      className="overflow-hidden rounded-xl border border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/60"
      data-testid={`policy-waiver-${waiver.waiver_id}`}
    >
      <div className="flex flex-wrap items-start gap-3 p-4">
        <button
          type="button"
          onClick={() => setExpanded((current) => !current)}
          aria-label={`${expanded ? 'Collapse' : 'Expand'} waiver ${waiver.waiver_id}`}
          className="mt-0.5 rounded-lg p-1 text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800"
        >
          {expanded
            ? <ChevronUp size={17} aria-hidden="true" />
            : <ChevronDown size={17} aria-hidden="true" />}
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold text-surface-900 dark:text-white">
              {waiver.metric_code}
            </h3>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${STATUS_TONE[waiver.status]}`}
            >
              {POLICY_WAIVER_STATUS_LABEL[waiver.status]}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                waiver.currentness === 'current'
                  ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-400/15 dark:text-emerald-200'
                  : 'bg-red-100 text-red-800 dark:bg-red-400/15 dark:text-red-200'
              }`}
            >
              {waiver.currentness}
            </span>
          </div>
          <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
            {formatPolicyToken(waiver.entity_type)} · {waiver.subject_id} · v
            {waiver.subject_version} · revision {waiver.waiver_revision}
          </p>
          <dl className="mt-2 grid gap-x-4 gap-y-1 text-[11px] text-surface-600 dark:text-surface-300 md:grid-cols-3">
            <div className="min-w-0">
              <dt className="font-semibold">Metric result</dt>
              <dd className="truncate font-mono" title={waiver.metric_result_id}>
                {waiver.metric_result_id}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="font-semibold">Finding</dt>
              <dd className="truncate font-mono" title={waiver.finding_id}>
                {waiver.finding_id}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="font-semibold">Receipt</dt>
              <dd className="truncate font-mono" title={waiver.receipt_id}>
                {waiver.receipt_id}
              </dd>
            </div>
          </dl>
        </div>
        {actions.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {actions.map((action) => (
              <button
                key={action}
                type="button"
                onClick={() => onAction(waiver, action)}
                className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-surface-300 px-2.5 text-xs font-semibold text-surface-700 hover:bg-surface-100 dark:border-surface-600 dark:text-surface-200 dark:hover:bg-surface-800"
              >
                <ActionIcon action={action} />
                {action === 'revalidate'
                  ? 'Revalidate'
                  : `${action[0]?.toUpperCase()}${action.slice(1)}`}
              </button>
            ))}
          </div>
        )}
      </div>
      {expanded && (
        <div className="space-y-3 border-t border-surface-200 bg-surface-50/60 p-4 dark:border-surface-700 dark:bg-surface-950/30">
          <div>
            <h4 className="text-xs font-semibold text-surface-700 dark:text-surface-200">
              Justification
            </h4>
            <p className="mt-1 whitespace-pre-wrap text-xs text-surface-600 dark:text-surface-300">
              {waiver.justification}
            </p>
          </div>
          <div>
            <h4 className="mb-2 text-xs font-semibold text-surface-700 dark:text-surface-200">
              Structured request evidence
            </h4>
            <EvidenceReferences references={waiver.evidence_refs} />
          </div>
          <dl className="grid gap-2 text-xs text-surface-600 dark:text-surface-300 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <dt className="font-semibold">Requested by</dt>
              <dd>{waiver.requested_by}</dd>
            </div>
            <div>
              <dt className="font-semibold">Requested at</dt>
              <dd>{formatPolicyTimestamp(waiver.requested_at)}</dd>
            </div>
            <div>
              <dt className="font-semibold">Expires at</dt>
              <dd>
                {waiver.expires_at
                  ? formatPolicyTimestamp(waiver.expires_at)
                  : 'No scheduled expiry'}
              </dd>
            </div>
            <div>
              <dt className="font-semibold">Assessment author</dt>
              <dd>{waiver.assessment_assessor_id}</dd>
            </div>
          </dl>
          {waiver.reviewed_by && (
            <p className="text-xs text-surface-600 dark:text-surface-300">
              Reviewed by {waiver.reviewed_by}
              {waiver.reviewed_at
                ? ` at ${formatPolicyTimestamp(waiver.reviewed_at)}`
                : ''}
              {waiver.review_reason ? ` · ${waiver.review_reason}` : ''}
            </p>
          )}
          {waiver.currentness_reasons.length > 0 && (
            <p className="text-xs text-red-700 dark:text-red-300">
              Currentness:{' '}
              {waiver.currentness_reasons.map(formatPolicyToken).join(', ')}
            </p>
          )}
          {waiver.expire_reason && (
            <p className="text-xs text-orange-700 dark:text-orange-300">
              Expiry reason:{' '}
              {policyWaiverExpireReasonLabel(waiver.expire_reason)}
            </p>
          )}
          <WaiverHistory
            boardId={boardId}
            evaluatedAt={evaluatedAt}
            waiver={waiver}
          />
        </div>
      )}
    </li>
  );
}

export function PolicyWaiverPanel({
  boardId,
}: {
  boardId: string;
}) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [status, setStatus] = useState<PolicyWaiverStatus | ''>('');
  const [entityType, setEntityType] =
    useState<PolicyEntityType | ''>('');
  const [filterDraft, setFilterDraft] = useState<WaiverFilters>({
    subjectId: '',
    metricResultId: '',
    findingId: '',
    receiptId: '',
  });
  const [filters, setFilters] = useState<WaiverFilters>(filterDraft);
  const [snapshot, setSnapshot] = useState(() => newSnapshot());
  const [selection, setSelection] =
    useState<WaiverActionSelection | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const authorityReady = (
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired
  );
  const canRead = (
    authorityReady
    && permissions.has('guidelines.waiver.read')
  );
  const canReview = (
    authorityReady
    && permissions.has('guidelines.waiver.review')
  );
  const canRevoke = (
    authorityReady
    && permissions.has('guidelines.waiver.revoke')
  );
  const canRevalidate = (
    authorityReady
    && permissions.has('guidelines.waiver.revalidate')
  );
  const resetKey = JSON.stringify([
    boardId,
    snapshot.evaluatedAt,
    snapshot.generation,
    status,
    entityType,
    filters,
  ]);

  const loadPage = useCallback(async (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => validatedSemanticWaiverPage(
    await api.listSemanticMetricWaivers(boardId, {
      evaluatedAt: snapshot.evaluatedAt,
      limit: WAIVER_PAGE_SIZE,
      projection: 'full',
      cursor,
      ...(status ? { status } : {}),
      ...(entityType ? { subjectType: entityType } : {}),
      ...(filters.subjectId
        ? { subjectId: filters.subjectId }
        : {}),
      ...(filters.metricResultId
        ? { metricResultId: filters.metricResultId }
        : {}),
      ...(filters.findingId ? { findingId: filters.findingId } : {}),
      ...(filters.receiptId ? { receiptId: filters.receiptId } : {}),
      signal,
    }),
    boardId,
    {
      evaluatedAt: snapshot.evaluatedAt,
      ...(status ? { status } : {}),
      ...(entityType ? { entityType } : {}),
      ...(filters.subjectId
        ? { subjectId: filters.subjectId }
        : {}),
      ...(filters.metricResultId
        ? { metricResultId: filters.metricResultId }
        : {}),
      ...(filters.findingId ? { findingId: filters.findingId } : {}),
      ...(filters.receiptId ? { receiptId: filters.receiptId } : {}),
    },
    WAIVER_PAGE_SIZE,
  ), [
    api,
    boardId,
    entityType,
    filters,
    snapshot.evaluatedAt,
    status,
  ]);

  const waivers = useOpaqueCursorCollection({
    enabled: canRead,
    resetKey,
    loadPage,
    getItemKey: (item: SemanticWaiverFull) => item.waiver_id,
    classifyError: classifyPolicyWaiverCursorError,
    duplicateItemMessage:
      'A semantic waiver identity repeated across cursor pages. Restart from the newest snapshot.',
    repeatedCursorMessage:
      'The semantic waiver cursor repeated. Restart from the newest snapshot.',
  });

  const refreshNewest = useCallback(() => {
    setMessage(null);
    setSnapshot((current) =>
      newSnapshot(current.generation + 1, current.evaluatedAt),
    );
  }, []);

  const applyFilters = () => {
    setMessage(null);
    setFilters({
      subjectId: filterDraft.subjectId.trim(),
      metricResultId: filterDraft.metricResultId.trim(),
      findingId: filterDraft.findingId.trim(),
      receiptId: filterDraft.receiptId.trim(),
    });
    setSnapshot((current) =>
      newSnapshot(current.generation + 1, current.evaluatedAt),
    );
  };

  const completeAction = async (result: PolicyWaiverMutationResult) => {
    setSelection(null);
    setMessage(
      `${formatPolicyToken(result.action)} completed for waiver `
      + `${result.waiverId} at revision ${result.waiverRevision}.`,
    );
    setSnapshot((current) =>
      newSnapshot(current.generation + 1, current.evaluatedAt),
    );
  };

  const filtersActive = Boolean(
    status
    || entityType
    || Object.values(filters).some(Boolean),
  );
  const authorityError = permissions.error
    ? 'Permission status is unavailable. Waiver evidence and actions fail closed.'
    : permissions.ownerReviewRequired
      ? 'Owner review is required before waiver evidence is available.'
      : !permissions.isLoading && !canRead
        ? 'guidelines.waiver.read is not granted.'
        : null;

  if (permissions.isLoading) {
    return (
      <section
        className="rounded-xl border border-surface-200 p-4 dark:border-surface-700"
        data-testid="policy-waiver-panel"
        aria-busy="true"
      >
        <div className="mb-2 flex justify-end">
          <ContextualHelpLink
            sectionId="policy-governance"
            testId="policy-waiver-help"
          >
            How waivers work
          </ContextualHelpLink>
        </div>
        <p role="status" className="text-sm text-surface-500">
          Checking semantic waiver management access…
        </p>
      </section>
    );
  }

  if (!canRead) {
    return (
      <section
        className="rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950/30"
        data-testid="policy-waiver-panel"
      >
        <div className="mb-2 flex justify-end">
          <ContextualHelpLink
            sectionId="policy-governance"
            testId="policy-waiver-help"
          >
            How waivers work
          </ContextualHelpLink>
        </div>
        <p role="alert" className="text-sm text-red-700 dark:text-red-300">
          {authorityError}
        </p>
      </section>
    );
  }

  return (
    <div className="space-y-4" data-testid="policy-waiver-panel">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-base font-semibold text-surface-900 dark:text-white">
            <ShieldCheck
              size={18}
              className="text-amber-500"
              aria-hidden="true"
            />
            Semantic metric waivers
          </h2>
          <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
            Govern exact metric results through independent review,
            revocation and explicit currentness revalidation.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ContextualHelpLink
            sectionId="policy-governance"
            testId="policy-waiver-help"
          >
            How waivers work
          </ContextualHelpLink>
          <button
            type="button"
            onClick={refreshNewest}
            disabled={waivers.loading}
            className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-surface-300 px-2.5 py-1 text-xs font-semibold text-surface-700 hover:bg-surface-100 disabled:opacity-50 dark:border-surface-600 dark:text-surface-200 dark:hover:bg-surface-800"
          >
            <RefreshCw
              size={13}
              className={waivers.loading ? 'animate-spin' : ''}
              aria-hidden="true"
            />
            Refresh newest
          </button>
        </div>
      </header>

      <section className="rounded-xl border border-surface-200 bg-surface-50 p-3 dark:border-surface-700 dark:bg-surface-950/30">
        <div className="flex items-center gap-2">
          <Filter size={14} className="text-surface-500" aria-hidden="true" />
          <h3 className="text-xs font-semibold uppercase tracking-wide text-surface-600 dark:text-surface-300">
            Exact server filters
          </h3>
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <label className="text-xs font-semibold text-surface-600 dark:text-surface-300">
            Status
            <select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value as PolicyWaiverStatus | '');
                setSnapshot((current) =>
                  newSnapshot(
                    current.generation + 1,
                    current.evaluatedAt,
                  ),
                );
              }}
              className="mt-1 block w-full rounded-lg border border-surface-300 bg-white px-2 py-2 text-xs dark:border-surface-700 dark:bg-surface-900"
            >
              <option value="">All statuses</option>
              {STATUS_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {POLICY_WAIVER_STATUS_LABEL[option]}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs font-semibold text-surface-600 dark:text-surface-300">
            Entity type
            <select
              value={entityType}
              onChange={(event) => {
                setEntityType(event.target.value as PolicyEntityType | '');
                setSnapshot((current) =>
                  newSnapshot(
                    current.generation + 1,
                    current.evaluatedAt,
                  ),
                );
              }}
              className="mt-1 block w-full rounded-lg border border-surface-300 bg-white px-2 py-2 text-xs dark:border-surface-700 dark:bg-surface-900"
            >
              <option value="">All entity types</option>
              {ENTITY_TYPE_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {formatPolicyToken(option)}
                </option>
              ))}
            </select>
          </label>
          {([
            ['subjectId', 'Subject ID'],
            ['metricResultId', 'Metric result ID'],
            ['findingId', 'Finding ID'],
            ['receiptId', 'Receipt ID'],
          ] as const).map(([field, label]) => (
            <label
              key={field}
              className="text-xs font-semibold text-surface-600 dark:text-surface-300"
            >
              {label}
              <input
                value={filterDraft[field]}
                onChange={(event) =>
                  setFilterDraft((current) => ({
                    ...current,
                    [field]: event.target.value,
                  }))}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') applyFilters();
                }}
                placeholder={`Exact ${label.toLowerCase()}`}
                className="mt-1 block w-full rounded-lg border border-surface-300 bg-white px-3 py-2 text-xs dark:border-surface-700 dark:bg-surface-900"
              />
            </label>
          ))}
          <button
            type="button"
            onClick={applyFilters}
            className="self-end rounded-lg border border-surface-300 bg-white px-3 py-2 text-xs font-semibold text-surface-700 hover:bg-surface-100 dark:border-surface-600 dark:bg-surface-900 dark:text-surface-200"
          >
            Apply exact filters
          </button>
        </div>
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-surface-500">
          <Clock3 size={12} aria-hidden="true" />
          Snapshot evaluated at {formatPolicyTimestamp(snapshot.evaluatedAt)}.
          Pagination keeps this instant, projection and all filters fixed.
        </p>
      </section>

      <p className="flex items-start gap-2 rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs text-blue-800 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-200">
        <ShieldCheck
          size={14}
          className="mt-0.5 shrink-0"
          aria-hidden="true"
        />
        Review and revalidation require an actor independent from both the
        requester and the semantic assessment author. Unknown or partial
        evidence disables mutations.
      </p>

      {message && (
        <p
          role="status"
          className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-200"
        >
          {message}
        </p>
      )}

      {waivers.loading && !waivers.loaded ? (
        <p
          role="status"
          className="rounded-lg border border-surface-200 p-4 text-sm text-surface-500 dark:border-surface-700"
        >
          Loading semantic metric waivers…
        </p>
      ) : waivers.error && waivers.items.length === 0 ? null
        : waivers.items.length === 0 ? (
          <div className="rounded-xl border border-dashed border-surface-300 p-8 text-center dark:border-surface-700">
            <FileWarning
              size={30}
              className="mx-auto text-surface-300 dark:text-surface-600"
              aria-hidden="true"
            />
            <p className="mt-2 text-sm text-surface-500 dark:text-surface-400">
              {filtersActive
                ? 'No semantic waiver matches the active exact filters.'
                : 'No semantic metric waiver has been requested on this board.'}
            </p>
          </div>
        ) : (
          <ul className="space-y-3">
            {waivers.items.map((waiver) => (
              <WaiverRow
                key={waiver.waiver_id}
                boardId={boardId}
                evaluatedAt={snapshot.evaluatedAt}
                waiver={waiver}
                canReview={canReview}
                canRevoke={canRevoke}
                canRevalidate={canRevalidate}
                onAction={(item, action) =>
                  setSelection({
                    waiver: item,
                    action,
                    evaluatedAt: snapshot.evaluatedAt,
                  })}
              />
            ))}
          </ul>
        )}

      <CursorCollectionControls
        collectionLabel="semantic metric waivers"
        itemCount={waivers.items.length}
        hasMore={waivers.hasMore}
        loading={waivers.loading}
        error={waivers.error}
        restartRequired={waivers.restartRequired}
        onLoadMore={waivers.loadMore}
        onRetry={waivers.retry}
        onRestart={refreshNewest}
        testId="policy-waiver-cursor"
      />

      <p className="flex items-start gap-2 rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-950/30 dark:text-surface-300">
        <History
          size={14}
          className="mt-0.5 shrink-0"
          aria-hidden="true"
        />
        Waiver heads transition only through governed actions. The immutable
        event chain has no edit or delete control.
      </p>

      {selection && (
        <PolicyWaiverActionDialog
          boardId={boardId}
          evaluatedAt={selection.evaluatedAt}
          waiver={selection.waiver}
          action={selection.action}
          onClose={() => setSelection(null)}
          onCompleted={completeAction}
        />
      )}
    </div>
  );
}

export default PolicyWaiverPanel;
