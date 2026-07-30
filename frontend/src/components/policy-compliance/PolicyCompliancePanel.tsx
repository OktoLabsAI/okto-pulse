import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  CircleOff,
  Clock3,
  FileWarning,
  ListChecks,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react';

import { CollapsibleEvidenceSection } from '@/components/shared/CollapsibleEvidenceSection';
import { CursorCollectionControls } from '@/components/shared/CursorCollectionControls';
import { ContextualHelpLink } from '@/components/help';
import { useOpaqueCursorCollection } from '@/hooks/useOpaqueCursorCollection';
import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import type {
  PolicyComplianceFindingDetail,
  PolicyComplianceFindingListItem,
  PolicyComplianceReceiptSummary,
  PolicyComplianceState,
  PolicyEntityType,
} from '@/types/policy-governance';

import {
  POLICY_COMPLIANCE_STATE_PRESENTATION,
  classifyPolicyCursorError,
  createPolicyUiId,
  formatPolicyTimestamp,
  formatPolicyToken,
  isPolicyComplianceFindingDetailForReceipt,
  isPolicyComplianceReceiptSummaryForSubject,
  policyUiErrorMessage,
} from './policyComplianceModel';
import {
  PolicyWaiverRequestDialog,
  type PolicyWaiverMutationResult,
} from './PolicyWaiverDialogs';

const POLICY_COLLECTION_PAGE_SIZE = 25;

type OverviewState = {
  scope: string;
} & (
  | {
      status: 'idle' | 'loading';
      receipt: null;
      error: null;
    }
  | {
      status: 'ready';
      receipt: PolicyComplianceReceiptSummary | null;
      error: null;
    }
  | {
      status: 'error';
      receipt: null;
      error: string;
    }
);

export interface PolicyCompliancePanelProps {
  boardId: string;
  entityType: PolicyEntityType;
  subjectId: string;
  evaluationEnabled?: boolean;
  evaluationUnavailableReason?: string;
  onRequestWaiver?: (
    finding: PolicyComplianceFindingDetail,
  ) => void;
  onEvaluated?: (
    receipt: PolicyComplianceReceiptSummary | null,
  ) => void;
  onRefreshed?: (
    receipt: PolicyComplianceReceiptSummary | null,
  ) => void;
  refreshKey?: number;
}

const STATE_CARD_TONES: Record<
  'success' | 'danger' | 'warning' | 'neutral',
  string
> = {
  success:
    'border-emerald-200 bg-emerald-50/60 dark:border-emerald-800/60 dark:bg-emerald-950/20',
  danger:
    'border-red-200 bg-red-50/60 dark:border-red-800/60 dark:bg-red-950/20',
  warning:
    'border-amber-200 bg-amber-50/70 dark:border-amber-800/60 dark:bg-amber-950/20',
  neutral:
    'border-surface-200 bg-surface-50/70 dark:border-surface-700 dark:bg-surface-900/40',
};

const STATE_BADGE_TONES: Record<
  'success' | 'danger' | 'warning' | 'neutral',
  string
> = {
  success:
    'bg-emerald-100 text-emerald-700 dark:bg-emerald-400/15 dark:text-emerald-200',
  danger:
    'bg-red-100 text-red-700 dark:bg-red-400/15 dark:text-red-200',
  warning:
    'bg-amber-100 text-amber-700 dark:bg-amber-400/15 dark:text-amber-200',
  neutral:
    'bg-surface-200 text-surface-700 dark:bg-surface-700 dark:text-surface-200',
};

function assertCursorPageShape(
  page: unknown,
  contractName: string,
): asserts page is {
  items: unknown[];
  limit: number;
  has_more: boolean;
  next_cursor?: string;
} {
  if (
    typeof page !== 'object'
    || page === null
    || !Array.isArray((page as { items?: unknown }).items)
    || typeof (page as { limit?: unknown }).limit !== 'number'
    || !Number.isInteger((page as { limit: number }).limit)
    || (page as { limit: number }).limit < 1
    || (page as { limit: number }).limit > 200
    || (page as { items: unknown[] }).items.length
      > (page as { limit: number }).limit
    || typeof (page as { has_more?: unknown }).has_more !== 'boolean'
    || (
      (page as { has_more: boolean }).has_more
      && (
        (page as { items: unknown[] }).items.length === 0
        ||
        typeof (page as { next_cursor?: unknown }).next_cursor !== 'string'
        || !(page as { next_cursor: string }).next_cursor
      )
    )
    || (
      !(page as { has_more: boolean }).has_more
      && 'next_cursor' in page
    )
  ) {
    throw new Error(`${contractName} returned a malformed cursor page.`);
  }
}

function StateIcon({
  state,
}: {
  state: PolicyComplianceState;
}) {
  switch (state) {
    case 'ready':
      return (
        <CheckCircle2
          size={20}
          className="text-emerald-600 dark:text-emerald-300"
          aria-hidden="true"
        />
      );
    case 'blocked':
      return (
        <ShieldAlert
          size={20}
          className="text-red-600 dark:text-red-300"
          aria-hidden="true"
        />
      );
    case 'ready_with_waivers':
      return (
        <ShieldCheck
          size={20}
          className="text-amber-600 dark:text-amber-300"
          aria-hidden="true"
        />
      );
    case 'not_applicable':
      return (
        <CircleOff
          size={20}
          className="text-surface-500 dark:text-surface-300"
          aria-hidden="true"
        />
      );
  }
}

function CurrentPolicyReceipt({
  receipt,
}: {
  receipt: PolicyComplianceReceiptSummary;
}) {
  const presentation =
    POLICY_COMPLIANCE_STATE_PRESENTATION[receipt.state];
  const stale = receipt.currentness === 'stale';
  const tone = stale ? 'warning' : presentation.tone;

  return (
    <article
      className={`rounded-xl border p-4 ${STATE_CARD_TONES[tone]}`}
      data-testid="policy-compliance-current-receipt"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          {stale ? (
            <Clock3
              size={20}
              className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-300"
              aria-hidden="true"
            />
          ) : (
            <StateIcon state={receipt.state} />
          )}
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <strong className="text-sm text-surface-900 dark:text-white">
                {stale
                  ? 'Policy receipt is stale'
                  : presentation.headline}
              </strong>
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${STATE_BADGE_TONES[stale ? 'neutral' : presentation.tone]}`}
              >
                {stale
                  ? `Recorded state: ${presentation.label}`
                  : presentation.label}
              </span>
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                  stale
                    ? STATE_BADGE_TONES.warning
                    : STATE_BADGE_TONES.success
                }`}
              >
                {stale ? 'Stale evidence' : 'Current evidence'}
              </span>
            </div>
            <p className="mt-1 text-xs text-surface-600 dark:text-surface-300">
              {stale
                ? 'The recorded state is historical. Re-evaluate before relying on it for a governed transition.'
                : presentation.description}
            </p>
          </div>
        </div>
        <time
          dateTime={receipt.evaluated_at}
          className="text-[11px] text-surface-500 dark:text-surface-400"
        >
          {formatPolicyTimestamp(receipt.evaluated_at)}
        </time>
      </div>

      <dl
        className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4"
        aria-label="Policy Compliance counts"
      >
        {[
          ['Evaluated rules', receipt.rule_count],
          ['Policy findings', receipt.finding_count],
          ['Open blocking', receipt.blocking_finding_count],
          ['Waived', receipt.waived_finding_count],
        ].map(([label, value]) => (
          <div
            key={label}
            className="rounded-lg border border-current/10 bg-white/60 px-3 py-2 dark:bg-surface-950/20"
          >
            <dt className="text-[10px] font-medium uppercase tracking-wide text-surface-500 dark:text-surface-400">
              {label}
            </dt>
            <dd className="mt-0.5 text-base font-semibold text-surface-900 dark:text-white">
              {value}
            </dd>
          </div>
        ))}
      </dl>

      {(receipt.currentness_reasons.length > 0
        || receipt.reason_codes.length > 0) && (
        <div className="mt-3 space-y-1 border-t border-current/10 pt-3 text-xs">
          {receipt.currentness_reasons.length > 0 && (
            <p className="text-amber-800 dark:text-amber-200">
              Currentness: {receipt.currentness_reasons
                .map(formatPolicyToken)
                .join(', ')}
            </p>
          )}
          {receipt.reason_codes.length > 0 && (
            <p className="text-surface-600 dark:text-surface-300">
              Evaluation: {receipt.reason_codes
                .map(formatPolicyToken)
                .join(', ')}
            </p>
          )}
        </div>
      )}

      <p className="mt-3 break-all border-t border-current/10 pt-3 text-[11px] text-surface-500 dark:text-surface-400">
        Receipt {receipt.receipt_id} · subject v{receipt.subject.subject_version}
        {' '}· evaluator {receipt.evaluator_version}
      </p>
    </article>
  );
}

function PolicyReceiptHistory({
  receipts,
}: {
  receipts: PolicyComplianceReceiptSummary[];
}) {
  if (receipts.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
        No Policy Compliance receipt has been recorded for this subject.
      </p>
    );
  }

  return (
    <ol
      className="space-y-2"
      data-testid="policy-compliance-receipt-history"
    >
      {receipts.map((receipt) => {
        const presentation =
          POLICY_COMPLIANCE_STATE_PRESENTATION[receipt.state];
        return (
          <li
            key={receipt.receipt_id}
            className="rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/50"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${STATE_BADGE_TONES[presentation.tone]}`}
                  >
                    {presentation.label}
                  </span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                      receipt.currentness === 'current'
                        ? STATE_BADGE_TONES.success
                        : STATE_BADGE_TONES.warning
                    }`}
                  >
                    {formatPolicyToken(receipt.currentness)}
                  </span>
                </div>
                <p className="mt-1 text-xs text-surface-600 dark:text-surface-300">
                  {receipt.rule_count} rules · {receipt.finding_count} findings
                  {' '}· {receipt.blocking_finding_count} open blocking
                  {' '}· {receipt.waived_finding_count} waived
                </p>
              </div>
              <time
                dateTime={receipt.evaluated_at}
                className="text-[11px] text-surface-500 dark:text-surface-400"
              >
                {formatPolicyTimestamp(receipt.evaluated_at)}
              </time>
            </div>
            {receipt.currentness_reasons.length > 0 && (
              <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                {receipt.currentness_reasons
                  .map(formatPolicyToken)
                  .join(', ')}
              </p>
            )}
            <p className="mt-2 break-all text-[11px] text-surface-500 dark:text-surface-400">
              Receipt {receipt.receipt_id} · subject v{receipt.subject.subject_version}
              {' '}· outcome {formatPolicyToken(receipt.outcome)}
            </p>
          </li>
        );
      })}
    </ol>
  );
}

function findingTone(
  finding: PolicyComplianceFindingDetail,
): string {
  if (finding.blocking) {
    return 'bg-red-100 text-red-700 dark:bg-red-400/15 dark:text-red-200';
  }
  if (finding.waiver_id) {
    return 'bg-amber-100 text-amber-700 dark:bg-amber-400/15 dark:text-amber-200';
  }
  return 'bg-blue-100 text-blue-700 dark:bg-blue-400/15 dark:text-blue-200';
}

function PolicyFindingItems({
  findings,
  canRequestWaiver,
  onRequestWaiver,
}: {
  findings: PolicyComplianceFindingDetail[];
  canRequestWaiver: boolean;
  onRequestWaiver?: (
    finding: PolicyComplianceFindingDetail,
  ) => void;
}) {
  if (findings.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
        The latest receipt has no policy finding.
      </p>
    );
  }

  return (
    <ol
      className="space-y-2"
      data-testid="policy-compliance-findings"
    >
      {findings.map((finding) => (
        <li
          key={finding.finding_id}
          className="rounded-lg border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/50"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <span
                  className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${findingTone(finding)}`}
                >
                  {finding.blocking
                    ? 'Open blocking'
                    : finding.waiver_id
                      ? 'Waived'
                      : formatPolicyToken(finding.enforcement)}
                </span>
                <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[10px] font-medium text-surface-600 dark:bg-surface-800 dark:text-surface-300">
                  {formatPolicyToken(finding.outcome)}
                </span>
              </div>
              <p className="mt-2 whitespace-pre-wrap text-sm text-surface-800 dark:text-surface-100">
                {finding.message}
              </p>
            </div>
            <time
              dateTime={finding.created_at}
              className="text-[11px] text-surface-500 dark:text-surface-400"
            >
              {formatPolicyTimestamp(finding.created_at)}
            </time>
          </div>

          <p className="mt-2 break-all text-[11px] text-surface-500 dark:text-surface-400">
            Receipt {finding.receipt_id} · Rule {finding.rule_id}
            {' '}· Finding {finding.finding_id}
          </p>

          {finding.evidence_refs.length > 0 && (
            <div className="mt-2">
              <h5 className="text-[11px] font-semibold uppercase tracking-wide text-surface-500 dark:text-surface-400">
                Evidence references
              </h5>
              <ul className="mt-1 space-y-0.5">
                {finding.evidence_refs.map((reference) => (
                  <li
                    key={reference}
                    className="break-all text-xs text-surface-600 dark:text-surface-300"
                  >
                    {reference}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {finding.waiver_id ? (
            <p className="mt-2 break-all text-xs font-medium text-amber-700 dark:text-amber-300">
              Governed waiver {finding.waiver_id}
            </p>
          ) : (
            finding.outcome === 'fail'
            && canRequestWaiver
            && onRequestWaiver && (
              <div className="mt-3 flex justify-end">
                <button
                  type="button"
                  onClick={() => onRequestWaiver(finding)}
                  className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-3 py-1 text-xs font-semibold text-amber-800 hover:bg-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-200 dark:focus-visible:ring-offset-surface-900"
                >
                  <FileWarning size={14} aria-hidden="true" />
                  Request waiver
                </button>
              </div>
            )
          )}
        </li>
      ))}
    </ol>
  );
}

export function PolicyCompliancePanel({
  boardId,
  entityType,
  subjectId,
  evaluationEnabled = true,
  evaluationUnavailableReason,
  onRequestWaiver,
  onEvaluated,
  onRefreshed,
  refreshKey = 0,
}: PolicyCompliancePanelProps) {
  const subjectIdentitySignature = JSON.stringify([
    boardId,
    entityType,
    subjectId,
  ]);
  const subjectSignature = JSON.stringify([
    boardId,
    entityType,
    subjectId,
    refreshKey,
  ]);
  const overviewScope = subjectSignature;
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [overview, setOverview] = useState<OverviewState>({
    scope: overviewScope,
    status: 'idle',
    receipt: null,
    error: null,
  });
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [findingsExpanded, setFindingsExpanded] = useState(false);
  const [collectionVersion, setCollectionVersion] = useState(0);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluationError, setEvaluationError] = useState<string | null>(null);
  const [evaluationMessage, setEvaluationMessage] =
    useState<string | null>(null);
  const [waiverFinding, setWaiverFinding] =
    useState<PolicyComplianceFindingDetail | null>(null);
  const overviewRequestRef = useRef(0);
  const overviewControllerRef = useRef<AbortController | null>(null);
  const evaluationRequestRef = useRef(0);
  const evaluationControllerRef = useRef<AbortController | null>(null);
  const evaluationIntentRef = useRef({
    signature: '',
    idempotencyKey: createPolicyUiId('policy-evaluation'),
  });
  const evaluationHeadingId = useId();
  const evaluationReasonId = useId();

  const authorityReady =
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired;
  const canRead =
    authorityReady && permissions.has('guidelines.compliance.read');
  const canEvaluate =
    authorityReady && permissions.has('guidelines.compliance.evaluate');
  const canRequestWaiver =
    authorityReady && permissions.has('guidelines.waiver.request');
  const subjectScope = useMemo(
    () => ({ boardId, entityType, subjectId }),
    [boardId, entityType, subjectId],
  );
  const subjectSignatureRef = useRef(subjectSignature);
  subjectSignatureRef.current = subjectSignature;

  const loadOverview = useCallback(async () => {
    if (!canRead) {
      overviewControllerRef.current?.abort();
      setOverview({
        scope: overviewScope,
        status: 'idle',
        receipt: null,
        error: null,
      });
      return null;
    }

    const requestId = overviewRequestRef.current + 1;
    overviewRequestRef.current = requestId;
    overviewControllerRef.current?.abort();
    const controller = new AbortController();
    overviewControllerRef.current = controller;
    setOverview({
      scope: overviewScope,
      status: 'loading',
      receipt: null,
      error: null,
    });

    try {
      const page = await api.listPolicyComplianceReceipts(boardId, {
        limit: 1,
        projection: 'summary',
        entityType,
        subjectId,
        signal: controller.signal,
      });
      if (
        controller.signal.aborted
        || requestId !== overviewRequestRef.current
      ) {
        return null;
      }
      assertCursorPageShape(page, 'Policy Compliance overview');
      if (page.items.length > 1) {
        throw new Error(
          'Policy Compliance overview returned more than one latest receipt.',
        );
      }
      const receipt = page.items[0] ?? null;
      if (
        receipt !== null
        && !isPolicyComplianceReceiptSummaryForSubject(
          receipt,
          subjectScope,
        )
      ) {
        throw new Error(
          'Policy Compliance overview returned a malformed or mismatched receipt.',
        );
      }
      setOverview({
        scope: overviewScope,
        status: 'ready',
        receipt,
        error: null,
      });
      return receipt;
    } catch (caught) {
      if (
        controller.signal.aborted
        || requestId !== overviewRequestRef.current
      ) {
        return null;
      }
      setOverview({
        scope: overviewScope,
        status: 'error',
        receipt: null,
        error: policyUiErrorMessage(caught),
      });
      return null;
    }
  }, [
    api,
    boardId,
    canRead,
    entityType,
    overviewScope,
    subjectId,
    subjectScope,
  ]);

  useEffect(() => {
    void loadOverview();
    return () => overviewControllerRef.current?.abort();
  }, [loadOverview, refreshKey]);

  useEffect(() => {
    evaluationRequestRef.current += 1;
    evaluationControllerRef.current?.abort();
    evaluationIntentRef.current = {
      signature: subjectSignature,
      idempotencyKey: createPolicyUiId('policy-evaluation'),
    };
    setEvaluating(false);
    setEvaluationError(null);
    setEvaluationMessage(null);
    setCollectionVersion((value) => value + 1);
  }, [subjectSignature]);

  useEffect(() => {
    setHistoryExpanded(false);
    setFindingsExpanded(false);
  }, [subjectIdentitySignature]);

  useEffect(
    () => () => evaluationControllerRef.current?.abort(),
    [],
  );

  const activeOverview: OverviewState =
    overview.scope === overviewScope
      ? overview
      : {
          scope: overviewScope,
          status: 'idle',
          receipt: null,
          error: null,
        };

  const loadHistoryPage = useCallback(async (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => {
    const page = await api.listPolicyComplianceReceipts(boardId, {
      limit: POLICY_COLLECTION_PAGE_SIZE,
      projection: 'summary',
      entityType,
      subjectId,
      cursor,
      signal,
    });
    assertCursorPageShape(page, 'Policy Compliance receipt history');
    if (
      !page.items.every((item) =>
        isPolicyComplianceReceiptSummaryForSubject(item, subjectScope),
      )
    ) {
      throw new Error(
        'Policy Compliance receipt history returned a malformed or mismatched row.',
      );
    }
    return page as {
      items: PolicyComplianceReceiptSummary[];
      limit: number;
      has_more: boolean;
      next_cursor?: string;
    };
  }, [api, boardId, entityType, subjectId, subjectScope]);

  const history = useOpaqueCursorCollection({
    enabled: canRead && historyExpanded,
    resetKey: JSON.stringify([
      boardId,
      entityType,
      subjectId,
      collectionVersion,
      refreshKey,
      'history',
    ]),
    loadPage: loadHistoryPage,
    getItemKey: (item: PolicyComplianceReceiptSummary) => item.receipt_id,
    classifyError: classifyPolicyCursorError,
    duplicateItemMessage:
      'A receipt identity was repeated across cursor pages. Restart from the newest evidence.',
    repeatedCursorMessage:
      'The receipt cursor repeated. Restart from the newest evidence.',
  });

  const latestReceiptId = activeOverview.status === 'ready'
    ? activeOverview.receipt?.receipt_id ?? null
    : null;
  const loadFindingPage = useCallback(async (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => {
    if (!latestReceiptId) {
      return {
        items: [] as PolicyComplianceFindingDetail[],
        limit: POLICY_COLLECTION_PAGE_SIZE,
        has_more: false,
      };
    }
    const expected = {
      ...subjectScope,
      receiptId: latestReceiptId,
    };
    const page = await api.listPolicyComplianceFindings(boardId, {
      limit: POLICY_COLLECTION_PAGE_SIZE,
      projection: 'detail',
      receiptId: latestReceiptId,
      subjectId,
      cursor,
      signal,
    });
    assertCursorPageShape(page, 'Policy Compliance findings');
    if (
      !page.items.every((item: PolicyComplianceFindingListItem) =>
        isPolicyComplianceFindingDetailForReceipt(item, expected),
      )
    ) {
      throw new Error(
        'Policy Compliance findings returned a malformed or mismatched row.',
      );
    }
    return page as {
      items: PolicyComplianceFindingDetail[];
      limit: number;
      has_more: boolean;
      next_cursor?: string;
    };
  }, [
    api,
    boardId,
    latestReceiptId,
    subjectId,
    subjectScope,
  ]);

  const findings = useOpaqueCursorCollection({
    enabled:
      canRead
      && findingsExpanded
      && latestReceiptId !== null,
    resetKey: JSON.stringify([
      boardId,
      entityType,
      subjectId,
      latestReceiptId,
      collectionVersion,
      refreshKey,
      'findings',
    ]),
    loadPage: loadFindingPage,
    getItemKey: (item: PolicyComplianceFindingDetail) => item.finding_id,
    classifyError: classifyPolicyCursorError,
    duplicateItemMessage:
      'A finding identity was repeated across cursor pages. Restart the findings list.',
    repeatedCursorMessage:
      'The findings cursor repeated. Restart the findings list.',
  });

  const refreshEvidence = useCallback(async () => {
    setEvaluationError(null);
    setEvaluationMessage(null);
    const receipt = await loadOverview();
    setCollectionVersion((value) => value + 1);
    onRefreshed?.(receipt);
    return receipt;
  }, [loadOverview, onRefreshed]);

  const handleWaiverRequested = useCallback(async (
    result: PolicyWaiverMutationResult,
  ) => {
    setWaiverFinding(null);
    await refreshEvidence();
    setEvaluationMessage(
      `Waiver ${result.waiver.waiver_id} was requested at revision `
      + `${result.waiver.waiver_revision}; independent review is still `
      + 'required before it can become effective.',
    );
  }, [refreshEvidence]);

  const evaluate = async () => {
    if (
      !canEvaluate
      || !evaluationEnabled
      || evaluating
      || activeOverview.status !== 'ready'
    ) {
      return;
    }

    if (evaluationIntentRef.current.signature !== subjectSignature) {
      evaluationIntentRef.current = {
        signature: subjectSignature,
        idempotencyKey: createPolicyUiId('policy-evaluation'),
      };
    }

    const requestId = evaluationRequestRef.current + 1;
    evaluationRequestRef.current = requestId;
    evaluationControllerRef.current?.abort();
    const controller = new AbortController();
    evaluationControllerRef.current = controller;
    const evaluatedSubjectSignature = subjectSignature;
    setEvaluating(true);
    setEvaluationError(null);
    setEvaluationMessage(null);
    try {
      await api.evaluatePolicyCompliance(boardId, {
        entity_type: entityType,
        subject_id: subjectId,
        idempotency_key:
          evaluationIntentRef.current.idempotencyKey,
      }, controller.signal);
      if (
        controller.signal.aborted
        || requestId !== evaluationRequestRef.current
        || subjectSignatureRef.current !== evaluatedSubjectSignature
      ) {
        return;
      }
      evaluationIntentRef.current = {
        signature: subjectSignature,
        idempotencyKey: createPolicyUiId('policy-evaluation'),
      };
      const receipt = await loadOverview();
      if (
        controller.signal.aborted
        || requestId !== evaluationRequestRef.current
        || subjectSignatureRef.current !== evaluatedSubjectSignature
      ) {
        return;
      }
      setCollectionVersion((value) => value + 1);
      setEvaluationMessage(
        receipt
          ? 'Evaluation recorded and authoritative currentness refreshed.'
          : 'Evaluation recorded; no current receipt is available after refresh.',
      );
      onEvaluated?.(receipt);
    } catch (caught) {
      if (
        controller.signal.aborted
        || requestId !== evaluationRequestRef.current
        || subjectSignatureRef.current !== evaluatedSubjectSignature
      ) {
        return;
      }
      setEvaluationError(policyUiErrorMessage(caught));
      if (
        caught instanceof PolicyGovernanceApiError
        && caught.kind === 'conflict'
      ) {
        evaluationIntentRef.current = {
          signature: subjectSignature,
          idempotencyKey: createPolicyUiId('policy-evaluation'),
        };
        void loadOverview();
      }
    } finally {
      if (
        requestId === evaluationRequestRef.current
        && subjectSignatureRef.current === evaluatedSubjectSignature
      ) {
        setEvaluating(false);
      }
    }
  };

  if (permissions.isLoading) {
    return (
      <section
        className="rounded-xl border border-surface-200 p-4 dark:border-surface-700"
        data-testid="policy-compliance-panel"
        aria-busy="true"
      >
        <div className="mb-2 flex justify-end">
          <ContextualHelpLink
            sectionId="policy-governance"
            testId="policy-compliance-help"
          >
            How policy works
          </ContextualHelpLink>
        </div>
        <p
          role="status"
          className="text-sm text-surface-500 dark:text-surface-400"
        >
          Checking Policy Compliance access…
        </p>
      </section>
    );
  }

  if (permissions.error || permissions.ownerReviewRequired) {
    return (
      <section
        className="rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950/30"
        data-testid="policy-compliance-panel"
      >
        <div className="mb-2 flex justify-end">
          <ContextualHelpLink
            sectionId="policy-governance"
            testId="policy-compliance-help"
          >
            How policy works
          </ContextualHelpLink>
        </div>
        <p role="alert" className="text-sm text-red-700 dark:text-red-300">
          Policy Compliance is unavailable because effective authority could
          not be verified. No evidence was loaded and no action is enabled.
        </p>
      </section>
    );
  }

  if (!canRead) {
    return (
      <section
        className="rounded-xl border border-surface-200 bg-surface-50 p-4 dark:border-surface-700 dark:bg-surface-900/40"
        data-testid="policy-compliance-panel"
      >
        <div className="mb-2 flex justify-end">
          <ContextualHelpLink
            sectionId="policy-governance"
            testId="policy-compliance-help"
          >
            How policy works
          </ContextualHelpLink>
        </div>
        <p className="text-sm text-surface-600 dark:text-surface-300">
          Policy Compliance is hidden because
          {' '}<code>guidelines.compliance.read</code> is not granted.
        </p>
      </section>
    );
  }

  const evaluateDisabled =
    !canEvaluate
    || !evaluationEnabled
    || evaluating
    || activeOverview.status !== 'ready';
  const visibleEvaluationReason = !evaluationEnabled
    ? evaluationUnavailableReason
      ?? 'Evaluation is unavailable for the current lifecycle state.'
    : !canEvaluate
      ? 'Read-only: guidelines.compliance.evaluate is not granted.'
      : activeOverview.status === 'error'
        ? 'Evaluation is disabled until current evidence can be loaded.'
        : activeOverview.status !== 'ready'
          ? 'Evaluation is disabled while current evidence is loading.'
        : null;

  const requestWaiver = onRequestWaiver ?? setWaiverFinding;

  return (
    <>
      <div
        className="space-y-5"
        data-testid="policy-compliance-panel"
      >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-base font-semibold text-surface-900 dark:text-white">
            <ShieldCheck
              size={18}
              className="text-violet-600 dark:text-violet-300"
              aria-hidden="true"
            />
            Policy Compliance
          </h3>
          <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
            Deterministic evidence from the exact guideline revisions adopted
            by this board. This is separate from Quality Assessment.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ContextualHelpLink
            sectionId="policy-governance"
            testId="policy-compliance-help"
          >
            How policy works
          </ContextualHelpLink>
          <button
            type="button"
            onClick={() => void refreshEvidence()}
            disabled={activeOverview.status === 'loading' || evaluating}
            className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-surface-300 bg-white px-2.5 py-1 text-xs text-surface-700 hover:bg-surface-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
          >
            <RefreshCw
              size={13}
              className={
                activeOverview.status === 'loading' ? 'animate-spin' : ''
              }
              aria-hidden="true"
            />
            Refresh
          </button>
        </div>
      </header>

      <section
        className="space-y-2"
        aria-busy={activeOverview.status === 'loading'}
      >
        <h4 className="text-sm font-semibold text-surface-800 dark:text-surface-100">
          Latest receipt
        </h4>
        {activeOverview.status === 'loading'
          || activeOverview.status === 'idle' ? (
          <p
            role="status"
            className="rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
          >
            Loading authoritative Policy Compliance evidence…
          </p>
        ) : activeOverview.status === 'error' ? (
          <div
            role="alert"
            className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
          >
            Could not load Policy Compliance evidence. {activeOverview.error}
          </div>
        ) : activeOverview.receipt ? (
          <CurrentPolicyReceipt receipt={activeOverview.receipt} />
        ) : (
          <p
            className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
            data-testid="policy-compliance-empty"
          >
            This subject has not been evaluated against the adopted policy set.
          </p>
        )}
      </section>

      <section
        className="rounded-xl border border-violet-200 bg-violet-50/50 p-3 dark:border-violet-800/60 dark:bg-violet-950/20"
        aria-labelledby={evaluationHeadingId}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h4
              id={evaluationHeadingId}
              className="text-sm font-semibold text-violet-900 dark:text-violet-100"
            >
              Deterministic evaluation
            </h4>
            <p className="mt-0.5 text-xs text-violet-700 dark:text-violet-300">
              The server owns subject versions, digests, currentness and the
              final compliance state.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void evaluate()}
            disabled={evaluateDisabled}
            aria-describedby={
              visibleEvaluationReason ? evaluationReasonId : undefined
            }
            className="inline-flex min-h-9 items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <ListChecks size={15} aria-hidden="true" />
            {evaluating ? 'Evaluating…' : 'Evaluate policies'}
          </button>
        </div>
        {visibleEvaluationReason && (
          <p
            id={evaluationReasonId}
            className="mt-2 text-xs text-violet-700 dark:text-violet-300"
          >
            {visibleEvaluationReason}
          </p>
        )}
        {evaluationError && (
          <p
            role="alert"
            className="mt-2 text-xs text-red-700 dark:text-red-300"
          >
            Evaluation failed. {evaluationError}
          </p>
        )}
        {evaluationMessage && (
          <p
            role="status"
            className="mt-2 text-xs text-emerald-700 dark:text-emerald-300"
          >
            {evaluationMessage}
          </p>
        )}
      </section>

      <CollapsibleEvidenceSection
        title="Receipt history"
        description="Append-only evidence ordered newest first with live currentness."
        expanded={historyExpanded}
        onToggle={() => setHistoryExpanded((value) => !value)}
        testId="policy-compliance-history"
      >
        {history.loading && !history.loaded ? (
          <p
            role="status"
            className="rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
          >
            Loading receipt history…
          </p>
        ) : history.error && history.items.length === 0 ? null : (
          <PolicyReceiptHistory receipts={history.items} />
        )}
        <CursorCollectionControls
          collectionLabel="receipt history"
          itemCount={history.items.length}
          hasMore={history.hasMore}
          loading={history.loading}
          error={history.error}
          restartRequired={history.restartRequired}
          onLoadMore={history.loadMore}
          onRetry={history.retry}
          onRestart={history.restart}
          testId="policy-compliance-history-cursor"
        />
      </CollapsibleEvidenceSection>

      <CollapsibleEvidenceSection
        title="Pinpoint policy findings"
        description="Stable receipt and rule identities with governed evidence; no Quality score is inferred."
        expanded={findingsExpanded}
        onToggle={() => setFindingsExpanded((value) => !value)}
        testId="policy-compliance-findings"
      >
        {!latestReceiptId ? (
          <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
            Evaluate this subject before inspecting policy findings.
          </p>
        ) : (
          <>
            {findings.loading && !findings.loaded ? (
              <p
                role="status"
                className="rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
              >
                Loading policy findings…
              </p>
            ) : findings.error && findings.items.length === 0 ? null : (
              <PolicyFindingItems
                findings={findings.items}
                canRequestWaiver={canRequestWaiver}
                onRequestWaiver={requestWaiver}
              />
            )}
            <CursorCollectionControls
              collectionLabel="policy findings"
              itemCount={findings.items.length}
              hasMore={findings.hasMore}
              loading={findings.loading}
              error={findings.error}
              restartRequired={findings.restartRequired}
              onLoadMore={findings.loadMore}
              onRetry={findings.retry}
              onRestart={findings.restart}
              testId="policy-compliance-findings-cursor"
            />
          </>
        )}
      </CollapsibleEvidenceSection>

      <p className="flex items-start gap-1.5 rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-300">
        <AlertTriangle
          size={14}
          className="mt-0.5 shrink-0"
          aria-hidden="true"
        />
        Policy Compliance uses its authoritative state in supported lifecycle
        gates. It does not create a second workflow and does not reuse Quality
        Assessment scores.
      </p>
      </div>
      {waiverFinding && !onRequestWaiver && (
        <PolicyWaiverRequestDialog
          boardId={boardId}
          finding={waiverFinding}
          onClose={() => setWaiverFinding(null)}
          onCompleted={handleWaiverRequested}
        />
      )}
    </>
  );
}

export default PolicyCompliancePanel;
