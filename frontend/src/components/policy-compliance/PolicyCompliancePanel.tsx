import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  RefreshCw,
  ShieldCheck,
  ShieldX,
} from 'lucide-react';

import { ContextualHelpLink } from '@/components/help';
import { CollapsibleEvidenceSection } from '@/components/shared/CollapsibleEvidenceSection';
import { CursorCollectionControls } from '@/components/shared/CursorCollectionControls';
import { MetricScoreRing } from '@/components/shared/MetricScoreRing';
import { useOpaqueCursorCollection } from '@/hooks/useOpaqueCursorCollection';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import { useDashboardApi } from '@/services/api';
import type {
  GuidelineMetricDirection,
  NonEmptyArray,
  PolicyEntityType,
  SemanticAssessmentDetail,
  SemanticEvidenceRef,
  SemanticFindingDetail,
  SemanticSkipDetail,
  SemanticWaiverDetail,
} from '@/types/policy-governance';

import {
  parseSemanticAssessmentDetail,
  parseCreatedSemanticSkipResponse,
  parseSemanticDetailPage,
  parseSemanticFindingDetail,
  parseSemanticSkipDetail,
  parseSemanticWaiverDetail,
  parseRequestedSemanticWaiverResponse,
  parseRevokedSemanticSkipResponse,
  semanticMetricDirection,
  type SemanticSubjectExpectation,
} from './semanticPolicyModel';
import {
  projectPolicyTransitions,
  type PolicyTransitionPreviewLoadState,
} from './policyTransitionPreviewModel';

const PAGE_SIZE = 25;

export interface PolicyCompliancePanelProps {
  boardId: string;
  entityType: PolicyEntityType;
  subjectId: string;
  /**
   * Authoritative lifecycle revision currently rendered by the host modal.
   * Required for transition-decision skip creation when no receipt exists.
   */
  subjectVersion?: number;
  /**
   * Exact, already envelope-validated lifecycle authority for this subject.
   * Binding decisions allow a human skip before an admissible receipt exists.
   */
  transitionPreview?: PolicyTransitionPreviewLoadState;
  /**
   * Kept for host compatibility. Assessments are authored by agents through
   * the governed MCP/REST contract; this panel never invokes cognition.
   */
  evaluationEnabled?: boolean;
  evaluationUnavailableReason?: string;
  onRequestWaiver?: (finding: SemanticFindingDetail) => void;
  onEvaluated?: () => void;
  onRefreshed?: () => void;
  refreshKey?: number;
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString();
}

function shortIdentity(value: string): string {
  return value.length <= 18
    ? value
    : `${value.slice(0, 8)}…${value.slice(-6)}`;
}

interface TransitionSkipAuthority {
  bindingId: string;
  guidelineId: string;
  enforcement: 'advisory' | 'blocking';
  assessmentAvailable: boolean;
  inadmissibilityCause: string | null;
  skipped: boolean;
}

interface SkipCreateAuthority {
  bindingId: string;
  guidelineId: string;
  subjectVersion: number;
  source: 'assessment' | 'transition_decision';
}

function transitionSkipAuthorities(
  preview: PolicyTransitionPreviewLoadState | undefined,
): {
  items: TransitionSkipAuthority[];
  error: string | null;
} {
  if (!preview || preview.status !== 'ready') {
    return { items: [], error: null };
  }
  try {
    const projected = projectPolicyTransitions(preview.transitions);
    const byBinding = new Map<string, TransitionSkipAuthority>();
    for (const transition of projected.governed) {
      for (const decision of transition.decision.binding_decisions) {
        const candidate: TransitionSkipAuthority = {
          bindingId: decision.binding_id,
          guidelineId: decision.guideline_id,
          enforcement: decision.enforcement,
          assessmentAvailable: decision.assessment_available,
          inadmissibilityCause: decision.inadmissibility_cause,
          skipped: decision.skipped,
        };
        const existing = byBinding.get(candidate.bindingId);
        if (
          existing
          && (
            existing.guidelineId !== candidate.guidelineId
            || existing.enforcement !== candidate.enforcement
            || existing.assessmentAvailable
              !== candidate.assessmentAvailable
            || existing.inadmissibilityCause
              !== candidate.inadmissibilityCause
            || existing.skipped !== candidate.skipped
          )
        ) {
          throw new Error(
            'Lifecycle authority returned conflicting snapshots for one guideline binding.',
          );
        }
        if (!existing) byBinding.set(candidate.bindingId, candidate);
      }
    }
    return {
      items: [...byBinding.values()].sort((left, right) =>
        left.bindingId.localeCompare(right.bindingId)
      ),
      error: null,
    };
  } catch (caught) {
    return {
      items: [],
      error: caught instanceof Error
        ? caught.message
        : 'Lifecycle binding authority is malformed.',
    };
  }
}

function semanticError(error: unknown): {
  message: string;
  restartRequired: boolean;
} {
  if (error instanceof PolicyGovernanceApiError) {
    const action = error.nextAction
      ? ` Next: ${error.nextAction}.`
      : '';
    return {
      message: `${error.message}${action}`,
      restartRequired: error.kind === 'invalid_cursor',
    };
  }
  return {
    message:
      error instanceof Error
        ? error.message
        : 'Semantic guideline evidence could not be verified.',
    restartRequired: false,
  };
}

function toneForAssessment(
  assessment: SemanticAssessmentDetail,
): string {
  if (
    assessment.currentness === 'stale'
    || !assessment.confidence_admissible
    || !assessment.assessor_independent
  ) {
    return 'border-amber-300 bg-amber-50/50 dark:border-amber-800 dark:bg-amber-950/20';
  }
  return assessment.state === 'passed'
    ? 'border-emerald-300 bg-emerald-50/40 dark:border-emerald-800 dark:bg-emerald-950/20'
    : 'border-red-300 bg-red-50/40 dark:border-red-800 dark:bg-red-950/20';
}

interface ComplianceMetricAuthority {
  metricId: string;
  code: string;
  title: string;
  description: string;
  direction: GuidelineMetricDirection;
  effectiveThreshold: number;
  overridden: boolean;
}

interface BindingComplianceAuthority {
  bindingId: string;
  guidelineId: string;
  guidelineTitle: string;
  enforcement: 'advisory' | 'blocking';
  minimumConfidence: number | null;
  metrics: ComplianceMetricAuthority[];
}

type ComplianceAuthorityState =
  | { status: 'loading'; items: BindingComplianceAuthority[] }
  | { status: 'ready'; items: BindingComplianceAuthority[] }
  | {
      status: 'error';
      items: BindingComplianceAuthority[];
      message: string;
    };

function EnforcementBadge({
  enforcement,
}: {
  enforcement: 'advisory' | 'blocking';
}) {
  return (
    <span
      data-testid={`compliance-enforcement-${enforcement}`}
      className={
        enforcement === 'blocking'
          ? 'rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-700 dark:bg-red-400/15 dark:text-red-200'
          : 'rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:bg-amber-400/15 dark:text-amber-200'
      }
    >
      {enforcement}
    </span>
  );
}

function ComplianceStateChip({
  assessment,
}: {
  assessment: SemanticAssessmentDetail | null;
}) {
  if (!assessment) {
    return (
      <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-surface-600 dark:bg-surface-700/60 dark:text-surface-300">
        Not assessed
      </span>
    );
  }
  if (assessment.currentness === 'stale') {
    return (
      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:bg-amber-400/15 dark:text-amber-200">
        Stale
      </span>
    );
  }
  return assessment.state === 'passed' ? (
    <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700 dark:bg-emerald-400/15 dark:text-emerald-200">
      Passed
    </span>
  ) : (
    <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-700 dark:bg-red-400/15 dark:text-red-200">
      Failed
    </span>
  );
}

function CurrentnessBadge({
  currentness,
}: {
  currentness: 'current' | 'stale';
}) {
  return (
    <span
      className={
        currentness === 'current'
          ? 'rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700 dark:bg-emerald-400/15 dark:text-emerald-200'
          : 'rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:bg-amber-400/15 dark:text-amber-200'
      }
    >
      {currentness}
    </span>
  );
}

function EvidenceRefs({
  evidence,
}: {
  evidence: SemanticEvidenceRef[];
}) {
  return (
    <ul className="space-y-2">
      {evidence.map((item) => (
        <li
          key={`${item.source_type}:${item.source_id}:${item.source_version}:${item.content_hash}`}
          className="rounded-lg border border-surface-200 bg-surface-50 p-2 text-[11px] text-surface-600 dark:border-surface-700 dark:bg-surface-900/60 dark:text-surface-300"
        >
          <p className="font-semibold text-surface-800 dark:text-surface-100">
            {item.source_type} · {item.source_id} · v{item.source_version}
          </p>
          <code className="mt-1 block break-all text-[10px] text-surface-500 dark:text-surface-400">
            sha256:{item.content_hash}
          </code>
        </li>
      ))}
    </ul>
  );
}

function AssessmentCard({
  assessment,
  activeSkip,
  canManageSkips,
  onCreateSkip,
  onRevokeSkip,
}: {
  assessment: SemanticAssessmentDetail;
  activeSkip: SemanticSkipDetail | null;
  canManageSkips: boolean;
  onCreateSkip: (assessment: SemanticAssessmentDetail) => void;
  onRevokeSkip: (skip: SemanticSkipDetail) => void;
}) {
  return (
    <article
      className={`rounded-2xl border p-4 ${toneForAssessment(assessment)}`}
      data-testid="semantic-assessment-card"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h5 className="text-sm font-semibold text-surface-900 dark:text-white">
            Guideline {shortIdentity(assessment.guideline_id)}
          </h5>
          <p className="mt-1 text-[11px] text-surface-500 dark:text-surface-400">
            Binding {shortIdentity(assessment.binding_id)} · revision{' '}
            {assessment.binding_revision} · {assessment.enforcement}
          </p>
          <p className="mt-1 text-[11px] text-surface-500 dark:text-surface-400">
            Assessed by {assessment.assessor_agent_id}
            {assessment.assessor_model_id
              ? ` (${assessment.assessor_model_id})`
              : ''}
            {' '}at {formatTimestamp(assessment.recorded_at)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <CurrentnessBadge currentness={assessment.currentness} />
          <span
            className={
              assessment.state === 'passed'
                ? 'rounded-full bg-emerald-600 px-2 py-0.5 text-[10px] font-semibold uppercase text-white'
                : 'rounded-full bg-red-600 px-2 py-0.5 text-[10px] font-semibold uppercase text-white'
            }
          >
            {assessment.state === 'passed' ? 'Passed' : 'Threshold failed'}
          </span>
          {activeSkip && (
            <span className="rounded-full bg-violet-600 px-2 py-0.5 text-[10px] font-semibold uppercase text-white">
              Human skip active
            </span>
          )}
        </div>
      </header>

      {(assessment.currentness === 'stale'
        || !assessment.confidence_admissible
        || !assessment.assessor_independent) && (
        <div
          role="alert"
          className="mt-3 rounded-lg border border-amber-300 bg-white/70 p-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-surface-950/30 dark:text-amber-200"
        >
          {assessment.currentness === 'stale' && (
            <p>
              Stale: {assessment.currentness_reasons.join(', ')}.
            </p>
          )}
          {!assessment.confidence_admissible && (
            <p>
              Confidence is below the binding minimum; this receipt is
              inadmissible for a gate.
            </p>
          )}
          {!assessment.assessor_independent && (
            <p>
              Assessor separation was not satisfied; this receipt is
              inadmissible for a gate.
            </p>
          )}
        </div>
      )}

      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricScoreRing
          label="Confidence"
          value={assessment.confidence}
          direction="higher-is-better"
          threshold={assessment.minimum_confidence}
          testId="semantic-confidence-ring"
        />
        {assessment.metric_results.map((metric) => (
          <MetricScoreRing
            key={metric.metric_result_id}
            label={metric.metric_code}
            value={metric.score}
            direction={semanticMetricDirection(metric.direction)}
            threshold={metric.effective_threshold}
            testId={`semantic-metric-ring-${metric.metric_id}`}
          />
        ))}
      </div>

      <div className="mt-4 space-y-3">
        {assessment.metric_results.map((metric) => (
          <details
            key={metric.metric_result_id}
            className="rounded-xl border border-surface-200 bg-white/80 p-3 dark:border-surface-700 dark:bg-surface-900/50"
          >
            <summary className="cursor-pointer text-xs font-semibold text-surface-800 dark:text-surface-100">
              {metric.metric_code}: rationale, evidence and pinpoints
            </summary>
            <div className="mt-3 space-y-3 text-xs">
              <dl className="grid gap-2 sm:grid-cols-2">
                <div>
                  <dt className="text-surface-500 dark:text-surface-400">
                    Effective threshold
                  </dt>
                  <dd className="font-medium text-surface-800 dark:text-surface-100">
                    {metric.direction === 'minimum' ? 'Minimum' : 'Maximum'}{' '}
                    {metric.effective_threshold} ({metric.threshold_source})
                  </dd>
                </div>
                <div>
                  <dt className="text-surface-500 dark:text-surface-400">
                    Authoritative outcome
                  </dt>
                  <dd className="font-medium text-surface-800 dark:text-surface-100">
                    {metric.outcome}
                  </dd>
                </div>
              </dl>
              <div>
                <p className="font-semibold text-surface-700 dark:text-surface-200">
                  Rationale
                </p>
                <p className="mt-1 whitespace-pre-wrap text-surface-600 dark:text-surface-300">
                  {metric.rationale}
                </p>
              </div>
              <div>
                <p className="mb-2 font-semibold text-surface-700 dark:text-surface-200">
                  Evidence references
                </p>
                <EvidenceRefs evidence={metric.evidence_refs} />
              </div>
              <div>
                <p className="mb-2 font-semibold text-surface-700 dark:text-surface-200">
                  Pinpoints
                </p>
                <ul className="space-y-2">
                  {metric.pinpoints.map((pinpoint) => (
                    <li
                      key={`${pinpoint.anchor_type}:${pinpoint.anchor_ref ?? ''}:${pinpoint.input_digest}`}
                      className="rounded-lg border border-surface-200 bg-surface-50 p-2 text-surface-600 dark:border-surface-700 dark:bg-surface-900/60 dark:text-surface-300"
                    >
                      <span className="font-semibold">
                        {pinpoint.anchor_type}
                      </span>
                      {pinpoint.anchor_ref ? ` · ${pinpoint.anchor_ref}` : ''}
                      <code className="mt-1 block break-all text-[10px] text-surface-500 dark:text-surface-400">
                        input sha256:{pinpoint.input_digest}
                      </code>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </details>
        ))}
      </div>

      {canManageSkips && assessment.currentness === 'current' && (
        <div className="mt-4 flex justify-end">
          {activeSkip ? (
            <button
              type="button"
              onClick={() => onRevokeSkip(activeSkip)}
              className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-violet-300 bg-white px-3 py-1 text-xs font-semibold text-violet-700 hover:bg-violet-50 dark:border-violet-700 dark:bg-surface-900 dark:text-violet-200"
            >
              <ShieldX size={14} aria-hidden="true" />
              Revoke human skip
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onCreateSkip(assessment)}
              className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-violet-300 bg-white px-3 py-1 text-xs font-semibold text-violet-700 hover:bg-violet-50 dark:border-violet-700 dark:bg-surface-900 dark:text-violet-200"
            >
              <Ban size={14} aria-hidden="true" />
              Skip this binding
            </button>
          )}
        </div>
      )}
    </article>
  );
}

function Findings({
  items,
  canRequestWaiver,
  onRequestWaiver,
}: {
  items: SemanticFindingDetail[];
  canRequestWaiver: boolean;
  onRequestWaiver: (finding: SemanticFindingDetail) => void;
}) {
  if (items.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
        No failed semantic metric findings were recorded.
      </p>
    );
  }
  return (
    <ul className="space-y-3">
      {items.map((finding) => (
        <li
          key={finding.finding_id}
          className="rounded-xl border border-red-200 bg-red-50/40 p-3 dark:border-red-800 dark:bg-red-950/20"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-red-800 dark:text-red-200">
                {finding.metric_code}
              </p>
              <p className="mt-1 text-xs text-red-700 dark:text-red-300">
                {finding.rationale}
              </p>
            </div>
            <CurrentnessBadge currentness={finding.currentness} />
          </div>
          <details className="mt-3 text-xs">
            <summary className="cursor-pointer font-semibold text-surface-700 dark:text-surface-200">
              Evidence and pinpoints
            </summary>
            <div className="mt-2 space-y-3">
              <EvidenceRefs evidence={finding.evidence_refs} />
              <ul className="space-y-1 text-surface-600 dark:text-surface-300">
                {finding.pinpoints.map((pinpoint) => (
                  <li
                    key={`${pinpoint.anchor_type}:${pinpoint.anchor_ref ?? ''}:${pinpoint.input_digest}`}
                  >
                    {pinpoint.anchor_type}
                    {pinpoint.anchor_ref ? ` · ${pinpoint.anchor_ref}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          </details>
          {canRequestWaiver && finding.currentness === 'current' && (
            <div className="mt-3 flex justify-end">
              <button
                type="button"
                onClick={() => onRequestWaiver(finding)}
                className="min-h-8 rounded-lg border border-red-300 bg-white px-3 py-1 text-xs font-semibold text-red-700 hover:bg-red-50 dark:border-red-700 dark:bg-surface-900 dark:text-red-200"
              >
                Request metric waiver
              </button>
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function Waivers({ items }: { items: SemanticWaiverDetail[] }) {
  if (items.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
        No metric waivers exist for this subject.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {items.map((waiver) => (
        <li
          key={waiver.waiver_id}
          className="rounded-xl border border-surface-200 bg-white p-3 text-xs dark:border-surface-700 dark:bg-surface-900/40"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-semibold text-surface-800 dark:text-surface-100">
              {waiver.metric_code} · {waiver.status}
            </p>
            <CurrentnessBadge currentness={waiver.currentness} />
          </div>
          <p className="mt-2 text-surface-600 dark:text-surface-300">
            {waiver.justification}
          </p>
          <p className="mt-1 text-[11px] text-surface-500 dark:text-surface-400">
            Requested by {waiver.requested_by} at{' '}
            {formatTimestamp(waiver.requested_at)}
          </p>
        </li>
      ))}
    </ul>
  );
}

function Skips({
  items,
  canManage,
  onRevoke,
}: {
  items: SemanticSkipDetail[];
  canManage: boolean;
  onRevoke: (skip: SemanticSkipDetail) => void;
}) {
  if (items.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
        No human-owned binding skips exist for this subject.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {items.map((skip) => (
        <li
          key={skip.skip_id}
          className="rounded-xl border border-violet-200 bg-violet-50/40 p-3 text-xs dark:border-violet-800 dark:bg-violet-950/20"
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <p className="font-semibold text-violet-800 dark:text-violet-200">
              Binding {shortIdentity(skip.binding_id)} · {skip.status}
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <CurrentnessBadge currentness={skip.currentness} />
              {canManage && skip.status === 'active' && (
                <button
                  type="button"
                  onClick={() => onRevoke(skip)}
                  className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-violet-300 bg-white px-3 py-1 text-xs font-semibold text-violet-700 hover:bg-violet-50 dark:border-violet-700 dark:bg-surface-900 dark:text-violet-200"
                >
                  <ShieldX size={14} aria-hidden="true" />
                  Revoke human skip
                </button>
              )}
            </div>
          </div>
          <p className="mt-2 text-violet-700 dark:text-violet-300">
            {skip.reason}
          </p>
          <p className="mt-1 text-[11px] text-surface-500 dark:text-surface-400">
            Created by {skip.created_by} at {formatTimestamp(skip.created_at)}
          </p>
        </li>
      ))}
    </ul>
  );
}

function TransitionBindingSkips({
  items,
  error,
  subjectVersion,
  skipsReady,
  activeSkipByBinding,
  canManage,
  onCreate,
  onRevoke,
}: {
  items: TransitionSkipAuthority[];
  error: string | null;
  subjectVersion: number | null;
  skipsReady: boolean;
  activeSkipByBinding: ReadonlyMap<string, SemanticSkipDetail>;
  canManage: boolean;
  onCreate: (authority: SkipCreateAuthority) => void;
  onRevoke: (skip: SemanticSkipDetail) => void;
}) {
  if (!canManage || (items.length === 0 && error === null)) return null;
  return (
    <section
      className="space-y-3 rounded-xl border border-violet-200 bg-violet-50/30 p-4 dark:border-violet-800 dark:bg-violet-950/20"
      data-testid="transition-binding-skips"
    >
      <div>
        <h4 className="text-sm font-semibold text-violet-900 dark:text-violet-100">
          Human exceptions for lifecycle bindings
        </h4>
        <p className="mt-1 text-xs text-violet-700 dark:text-violet-300">
          These bindings come from the authoritative transition decision.
          A skip may be created even when its assessment is unavailable or
          inadmissible. The server resolves and seals the exact current
          binding revision and configuration.
        </p>
      </div>
      {error ? (
        <p
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
        >
          {error} Human skip creation is disabled.
        </p>
      ) : (
        <ul className="space-y-2">
          {items.map((binding) => {
            const activeSkip = activeSkipByBinding.get(binding.bindingId);
            const blockedByUnknownSkip =
              binding.skipped && activeSkip === undefined;
            const creationReady =
              subjectVersion !== null
              && skipsReady
              && !blockedByUnknownSkip;
            return (
              <li
                key={binding.bindingId}
                className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-violet-200 bg-white p-3 text-xs dark:border-violet-800 dark:bg-surface-900/50"
              >
                <div>
                  <p className="font-semibold text-surface-800 dark:text-surface-100">
                    Guideline {shortIdentity(binding.guidelineId)}
                  </p>
                  <p className="mt-1 text-surface-500 dark:text-surface-400">
                    Binding {shortIdentity(binding.bindingId)} ·{' '}
                    {binding.enforcement}
                    {' · '}
                    {binding.assessmentAvailable
                      ? binding.inadmissibilityCause
                        ? `inadmissible: ${binding.inadmissibilityCause}`
                        : 'assessment evidence available'
                      : 'assessment unavailable'}
                  </p>
                  {!creationReady && !activeSkip && (
                    <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
                      {subjectVersion === null
                        ? 'Creation is unavailable because this UI surface does not expose the authoritative subject revision.'
                        : !skipsReady
                          ? 'Load the complete human skip list before creating another exception.'
                          : 'The transition decision reports an active skip that is not present in the loaded list.'}
                    </p>
                  )}
                </div>
                {activeSkip ? (
                  <button
                    type="button"
                    onClick={() => onRevoke(activeSkip)}
                    className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-violet-300 bg-white px-3 py-1 text-xs font-semibold text-violet-700 hover:bg-violet-50 dark:border-violet-700 dark:bg-surface-900 dark:text-violet-200"
                  >
                    <ShieldX size={14} aria-hidden="true" />
                    Revoke human skip
                  </button>
                ) : (
                  <button
                    type="button"
                    disabled={!creationReady}
                    onClick={() => {
                      if (subjectVersion === null) return;
                      onCreate({
                        bindingId: binding.bindingId,
                        guidelineId: binding.guidelineId,
                        subjectVersion,
                        source: 'transition_decision',
                      });
                    }}
                    className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-violet-300 bg-white px-3 py-1 text-xs font-semibold text-violet-700 hover:bg-violet-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-violet-700 dark:bg-surface-900 dark:text-violet-200"
                  >
                    <Ban size={14} aria-hidden="true" />
                    Skip this binding
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function WaiverRequestDialog({
  boardId,
  finding,
  onClose,
  onCompleted,
}: {
  boardId: string;
  finding: SemanticFindingDetail;
  onClose: () => void;
  onCompleted: () => void;
}) {
  const api = usePolicyGovernanceApi();
  const [justification, setJustification] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const intentRef = useRef({
    signature: '',
    idempotencyKey: crypto.randomUUID(),
  });
  const focusTrap = useDialogFocusTrap(true, 'textarea');
  useEscapeToClose(onClose, {
    canClose: !submitting,
    priority: 30,
  });

  const submit = async () => {
    if (!justification.trim() || submitting) return;
    const signature = JSON.stringify([
      finding.finding_id,
      finding.metric_result_id,
      justification.trim(),
      expiresAt,
    ]);
    if (intentRef.current.signature !== signature) {
      intentRef.current = {
        signature,
        idempotencyKey: crypto.randomUUID(),
      };
    }
    setSubmitting(true);
    setError(null);
    try {
      parseRequestedSemanticWaiverResponse(
        await api.requestSemanticMetricWaiver(boardId, {
        metric_result_id: finding.metric_result_id,
        finding_id: finding.finding_id,
        receipt_id: finding.receipt_id,
        justification: justification.trim(),
        evidence_refs:
          finding.evidence_refs as NonEmptyArray<SemanticEvidenceRef>,
        expires_at: expiresAt
          ? new Date(`${expiresAt}T23:59:59.999Z`).toISOString()
          : null,
        idempotency_key: intentRef.current.idempotencyKey,
        }),
      );
      onCompleted();
    } catch (caught) {
      setError(semanticError(caught).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4">
      <div
        ref={focusTrap.dialogRef}
        onKeyDown={focusTrap.onKeyDown}
        role="dialog"
        aria-modal="true"
        aria-label="Request semantic metric waiver"
        tabIndex={-1}
        className="w-full max-w-lg rounded-2xl border border-surface-200 bg-white p-5 shadow-2xl dark:border-surface-700 dark:bg-surface-900"
      >
        <h4 className="text-base font-semibold text-surface-900 dark:text-white">
          Request waiver for {finding.metric_code}
        </h4>
        <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
          The immutable finding evidence is attached automatically. Independent
          review is required before a waiver can affect a gate.
        </p>
        <label className="mt-4 block text-xs font-semibold text-surface-700 dark:text-surface-200">
          Justification
          <textarea
            value={justification}
            onChange={(event) => setJustification(event.target.value)}
            rows={4}
            className="mt-1 w-full rounded-lg border border-surface-300 bg-white p-2 text-sm text-surface-900 dark:border-surface-600 dark:bg-surface-800 dark:text-white"
          />
        </label>
        <label className="mt-3 block text-xs font-semibold text-surface-700 dark:text-surface-200">
          Optional expiry date
          <input
            type="date"
            value={expiresAt}
            onChange={(event) => setExpiresAt(event.target.value)}
            className="mt-1 block rounded-lg border border-surface-300 bg-white px-2 py-1.5 text-sm text-surface-900 dark:border-surface-600 dark:bg-surface-800 dark:text-white"
          />
        </label>
        {error && (
          <p role="alert" className="mt-3 text-xs text-red-700 dark:text-red-300">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="min-h-9 rounded-lg border border-surface-300 px-3 text-xs font-semibold text-surface-700 dark:border-surface-600 dark:text-surface-200"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!justification.trim() || submitting}
            className="min-h-9 rounded-lg bg-violet-600 px-3 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? 'Requesting…' : 'Request waiver'}
          </button>
        </div>
      </div>
    </div>
  );
}

type SkipDialogState =
  | { mode: 'create'; authority: SkipCreateAuthority }
  | { mode: 'revoke'; skip: SemanticSkipDetail };

function SkipDialog({
  boardId,
  entityType,
  subjectId,
  state,
  onClose,
  onCompleted,
}: {
  boardId: string;
  entityType: PolicyEntityType;
  subjectId: string;
  state: SkipDialogState;
  onClose: () => void;
  onCompleted: () => void;
}) {
  const api = usePolicyGovernanceApi();
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const intentRef = useRef({
    signature: '',
    idempotencyKey: crypto.randomUUID(),
  });
  const focusTrap = useDialogFocusTrap(true, 'textarea');
  useEscapeToClose(onClose, {
    canClose: !submitting,
    priority: 30,
  });

  const submit = async () => {
    if (!reason.trim() || submitting) return;
    const signature = JSON.stringify([
      state.mode,
      state.mode === 'create'
        ? [
            state.authority.bindingId,
            state.authority.subjectVersion,
            state.authority.source,
          ]
        : state.skip.skip_id,
      reason.trim(),
    ]);
    if (intentRef.current.signature !== signature) {
      intentRef.current = {
        signature,
        idempotencyKey: crypto.randomUUID(),
      };
    }
    setSubmitting(true);
    setError(null);
    try {
      if (state.mode === 'create') {
        parseCreatedSemanticSkipResponse(
          await api.createSemanticPolicySkip(
            boardId,
            {
              subject_type: entityType,
              subject_id: subjectId,
              expected_subject_version: state.authority.subjectVersion,
              binding_id: state.authority.bindingId,
              reason: reason.trim(),
            },
            intentRef.current.idempotencyKey,
          ),
        );
      } else {
        parseRevokedSemanticSkipResponse(
          await api.revokeSemanticPolicySkip(
            boardId,
            state.skip.skip_id,
            {
              expected_skip_revision: state.skip.skip_revision,
              reason: reason.trim(),
              idempotency_key: intentRef.current.idempotencyKey,
            },
          ),
        );
      }
      onCompleted();
    } catch (caught) {
      setError(semanticError(caught).message);
    } finally {
      setSubmitting(false);
    }
  };

  const creating = state.mode === 'create';
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4">
      <div
        ref={focusTrap.dialogRef}
        onKeyDown={focusTrap.onKeyDown}
        role="dialog"
        aria-modal="true"
        aria-label={
          creating ? 'Skip guideline binding' : 'Revoke guideline skip'
        }
        tabIndex={-1}
        className="w-full max-w-lg rounded-2xl border border-surface-200 bg-white p-5 shadow-2xl dark:border-surface-700 dark:bg-surface-900"
      >
        <h4 className="text-base font-semibold text-surface-900 dark:text-white">
          {creating ? 'Skip this guideline binding' : 'Revoke human skip'}
        </h4>
        <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
          This is a human-owned, audited exception. Agents cannot create or
          revoke it, and subject or binding drift makes it stale.
        </p>
        <label className="mt-4 block text-xs font-semibold text-surface-700 dark:text-surface-200">
          Reason
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={4}
            className="mt-1 w-full rounded-lg border border-surface-300 bg-white p-2 text-sm text-surface-900 dark:border-surface-600 dark:bg-surface-800 dark:text-white"
          />
        </label>
        {error && (
          <p role="alert" className="mt-3 text-xs text-red-700 dark:text-red-300">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="min-h-9 rounded-lg border border-surface-300 px-3 text-xs font-semibold text-surface-700 dark:border-surface-600 dark:text-surface-200"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!reason.trim() || submitting}
            className="min-h-9 rounded-lg bg-violet-600 px-3 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting
              ? 'Saving…'
              : creating ? 'Create skip' : 'Revoke skip'}
          </button>
        </div>
      </div>
    </div>
  );
}

export function PolicyCompliancePanel({
  boardId,
  entityType,
  subjectId,
  subjectVersion,
  transitionPreview,
  evaluationEnabled = true,
  evaluationUnavailableReason,
  onRequestWaiver,
  onRefreshed,
  refreshKey = 0,
}: PolicyCompliancePanelProps) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [findingsExpanded, setFindingsExpanded] = useState(false);
  const [waiversExpanded, setWaiversExpanded] = useState(false);
  const [skipsExpanded, setSkipsExpanded] = useState(false);
  const [localRefresh, setLocalRefresh] = useState(0);
  const [waiverFinding, setWaiverFinding] =
    useState<SemanticFindingDetail | null>(null);
  const [skipDialog, setSkipDialog] = useState<SkipDialogState | null>(null);

  const authorityReady =
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired;
  const canRead =
    authorityReady && permissions.has('guidelines.assessments.read');
  const canReadWaivers =
    authorityReady && permissions.has('guidelines.waiver.read');
  const canRequestWaiver =
    authorityReady && permissions.has('guidelines.waiver.request');
  const canReadSkips =
    authorityReady && permissions.has('guidelines.adoption.manage');
  const canManageSkips = canReadSkips;
  const currentSubjectVersion =
    typeof subjectVersion === 'number'
    && Number.isInteger(subjectVersion)
    && subjectVersion > 0
      ? subjectVersion
      : null;
  const transitionBindings = useMemo(
    () => transitionSkipAuthorities(transitionPreview),
    [transitionPreview],
  );
  const transitionBindingIds = useMemo(
    () => new Set(
      transitionBindings.items.map((item) => item.bindingId),
    ),
    [transitionBindings.items],
  );

  const expectation = useMemo<SemanticSubjectExpectation>(
    () => ({ boardId, entityType, subjectId }),
    [boardId, entityType, subjectId],
  );
  const resetScope = JSON.stringify([
    boardId,
    entityType,
    subjectId,
    refreshKey,
    localRefresh,
  ]);
  const evaluatedAt = useMemo(
    () => new Date().toISOString(),
    [resetScope],
  );

  const dashboardApi = useDashboardApi();
  const [complianceAuthority, setComplianceAuthority] =
    useState<ComplianceAuthorityState>({ status: 'loading', items: [] });

  useEffect(() => {
    if (!canRead) return undefined;
    const controller = new AbortController();
    let cancelled = false;
    setComplianceAuthority({ status: 'loading', items: [] });
    (async () => {
      try {
        const entries = await dashboardApi.getBoardGuidelines(boardId);
        const items: BindingComplianceAuthority[] = [];
        for (const entry of entries) {
          if (
            !entry.binding_id
            || entry.binding_state === 'unlinked'
            || (
              entry.enforcement !== 'advisory'
              && entry.enforcement !== 'blocking'
            )
          ) {
            continue;
          }
          const revisionId = entry.guideline.revision_id;
          if (!revisionId) continue;
          const authority = await api.getGuidelineRevision(
            boardId,
            entry.guideline.id,
            revisionId,
            controller.signal,
          );
          const overrides = entry.metric_threshold_overrides ?? {};
          const metrics = authority.revision.metrics
            .filter((metric) =>
              metric.target_entity_types.includes(entityType)
            )
            .map((metric) => ({
              metricId: metric.metric_id,
              code: metric.code,
              title: metric.title,
              description: metric.description,
              direction: metric.direction,
              effectiveThreshold:
                overrides[metric.code] ?? metric.default_threshold,
              overridden: overrides[metric.code] !== undefined,
            }));
          if (metrics.length > 0) {
            items.push({
              bindingId: entry.binding_id,
              guidelineId: entry.guideline.id,
              guidelineTitle: entry.guideline.title,
              enforcement: entry.enforcement,
              minimumConfidence: entry.minimum_confidence ?? null,
              metrics,
            });
          }
        }
        if (!cancelled) {
          setComplianceAuthority({ status: 'ready', items });
        }
      } catch (caught) {
        if (!cancelled) {
          setComplianceAuthority({
            status: 'error',
            items: [],
            message:
              caught instanceof Error
                ? caught.message
                : 'Guideline authority could not be loaded.',
          });
        }
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // resetScope covers boardId/entityType/subjectId/refresh keys. The api
    // hooks are intentionally excluded: an unstable hook identity must never
    // refire this effect (root cause of the guidelines request loop).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canRead, resetScope]);

  useEffect(() => {
    setHistoryExpanded(false);
    setFindingsExpanded(false);
    setWaiversExpanded(false);
    setSkipsExpanded(false);
    setWaiverFinding(null);
    setSkipDialog(null);
  }, [boardId, entityType, subjectId]);

  const loadAssessmentPage = useCallback(async (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => parseSemanticDetailPage(
    await api.listSemanticGuidelineAssessments(boardId, {
      limit: PAGE_SIZE,
      cursor,
      projection: 'detail',
      subjectType: entityType,
      subjectId,
      signal,
    }),
    (item) => parseSemanticAssessmentDetail(item, expectation),
    PAGE_SIZE,
  ), [api, boardId, entityType, expectation, subjectId]);

  const assessments = useOpaqueCursorCollection({
    enabled: canRead,
    resetKey: `${resetScope}:assessments`,
    loadPage: loadAssessmentPage,
    getItemKey: (item: SemanticAssessmentDetail) => item.receipt_id,
    classifyError: semanticError,
    duplicateItemMessage:
      'A semantic assessment identity was repeated across cursor pages.',
    repeatedCursorMessage:
      'The semantic assessment cursor repeated. Restart from newest.',
  });

  const loadFindingPage = useCallback(async (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => parseSemanticDetailPage(
    await api.listSemanticGuidelineFindings(boardId, {
      limit: PAGE_SIZE,
      cursor,
      projection: 'detail',
      subjectType: entityType,
      subjectId,
      signal,
    }),
    (item) => parseSemanticFindingDetail(item, expectation),
    PAGE_SIZE,
  ), [api, boardId, entityType, expectation, subjectId]);

  const findings = useOpaqueCursorCollection({
    enabled: canRead && findingsExpanded,
    resetKey: `${resetScope}:findings`,
    loadPage: loadFindingPage,
    getItemKey: (item: SemanticFindingDetail) => item.finding_id,
    classifyError: semanticError,
  });

  const loadWaiverPage = useCallback(async (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => parseSemanticDetailPage(
    await api.listSemanticMetricWaivers(boardId, {
      limit: PAGE_SIZE,
      cursor,
      projection: 'detail',
      evaluatedAt,
      subjectType: entityType,
      subjectId,
      signal,
    }),
    (item) => parseSemanticWaiverDetail(item, expectation),
    PAGE_SIZE,
  ), [
    api,
    boardId,
    entityType,
    evaluatedAt,
    expectation,
    subjectId,
  ]);

  const waivers = useOpaqueCursorCollection({
    enabled: canReadWaivers && waiversExpanded,
    resetKey: `${resetScope}:waivers:${evaluatedAt}`,
    loadPage: loadWaiverPage,
    getItemKey: (item: SemanticWaiverDetail) => item.waiver_id,
    classifyError: semanticError,
  });

  const loadSkipPage = useCallback(async (
    cursor: string | undefined,
    signal: AbortSignal,
  ) => parseSemanticDetailPage(
    await api.listSemanticPolicySkips(boardId, {
      limit: PAGE_SIZE,
      cursor,
      projection: 'detail',
      subjectType: entityType,
      subjectId,
      signal,
    }),
    (item) => parseSemanticSkipDetail(item, expectation),
    PAGE_SIZE,
  ), [api, boardId, entityType, expectation, subjectId]);

  const skips = useOpaqueCursorCollection({
    enabled: canReadSkips,
    resetKey: `${resetScope}:skips`,
    loadPage: loadSkipPage,
    getItemKey: (item: SemanticSkipDetail) => item.skip_id,
    classifyError: semanticError,
  });

  const currentResolution = useMemo(() => {
    const byBinding = new Map<string, SemanticAssessmentDetail>();
    let error: string | null = null;
    for (const assessment of assessments.items) {
      const existing = byBinding.get(assessment.binding_id);
      if (
        assessment.currentness === 'current'
        && existing?.currentness === 'current'
      ) {
        error =
          'More than one current assessment was returned for the same binding.';
        break;
      }
      if (
        existing === undefined
        || (
          assessment.currentness === 'current'
          && existing.currentness === 'stale'
        )
      ) {
        byBinding.set(assessment.binding_id, assessment);
      }
    }
    return {
      items: [...byBinding.values()],
      error,
    };
  }, [assessments.items]);

  const currentAssessmentByBinding = useMemo(() => {
    const byBinding = new Map<string, SemanticAssessmentDetail>();
    for (const assessment of currentResolution.items) {
      byBinding.set(assessment.binding_id, assessment);
    }
    return byBinding;
  }, [currentResolution.items]);

  const activeSkipResolution = useMemo(() => {
    const result = new Map<string, SemanticSkipDetail>();
    let error: string | null = null;
    for (const skip of skips.items) {
      if (
        skip.status === 'active'
        && skip.currentness === 'current'
      ) {
        if (result.has(skip.binding_id)) {
          error =
            'More than one current active human skip was returned for the same binding.';
          break;
        }
        result.set(skip.binding_id, skip);
      }
    }
    return { items: result, error };
  }, [skips.items]);

  const refreshAll = useCallback(() => {
    setLocalRefresh((value) => value + 1);
    onRefreshed?.();
  }, [onRefreshed]);

  if (permissions.isLoading) {
    return (
      <section
        className="rounded-xl border border-surface-200 p-4 dark:border-surface-700"
        data-testid="policy-compliance-panel"
        aria-busy="true"
      >
        <p role="status" className="text-sm text-surface-500 dark:text-surface-400">
          Checking semantic guideline assessment access…
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
        <p role="alert" className="text-sm text-red-700 dark:text-red-300">
          Semantic guideline evidence is unavailable because effective
          authority could not be verified. The panel is fail-closed.
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
        <p className="text-sm text-surface-600 dark:text-surface-300">
          Semantic guideline assessments are hidden because{' '}
          <code>guidelines.assessments.read</code> is not granted.
        </p>
      </section>
    );
  }

  const requestWaiver = onRequestWaiver ?? setWaiverFinding;
  const completeCurrentSet =
    assessments.loaded
    && !assessments.loading
    && !assessments.error
    && !assessments.hasMore
    && currentResolution.error === null;
  const gateEvidenceReady =
    completeCurrentSet
    && currentResolution.items.length > 0
    && currentResolution.items.every(
      (assessment) =>
        assessment.currentness === 'current'
        && assessment.confidence_admissible
        && assessment.assessor_independent,
    );

  return (
    <>
      <div className="space-y-5" data-testid="policy-compliance-panel">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-2 text-base font-semibold text-surface-900 dark:text-white">
              <ShieldCheck
                size={18}
                className="text-violet-600 dark:text-violet-300"
                aria-hidden="true"
              />
              Semantic guideline assessments
            </h3>
            <p className="mt-1 max-w-3xl text-xs text-surface-500 dark:text-surface-400">
              Metric scores are recorded by agents against the adopted
              guideline revisions; Pulse verifies and seals them.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <ContextualHelpLink
              sectionId="policy-governance"
              testId="policy-compliance-help"
            >
              How semantic gates work
            </ContextualHelpLink>
            <button
              type="button"
              onClick={refreshAll}
              disabled={assessments.loading}
              className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-surface-300 bg-white px-2.5 py-1 text-xs text-surface-700 hover:bg-surface-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
            >
              <RefreshCw
                size={13}
                className={assessments.loading ? 'animate-spin' : ''}
                aria-hidden="true"
              />
              Refresh
            </button>
          </div>
        </header>

        <section
          className="space-y-3"
          data-testid="guideline-compliance-summary"
        >
          {complianceAuthority.status === 'loading' ? (
            <p
              role="status"
              className="rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
            >
              Loading guideline compliance…
            </p>
          ) : complianceAuthority.status === 'error' ? (
            <p
              role="alert"
              className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
            >
              Guideline authority could not be loaded:{' '}
              {complianceAuthority.message}
            </p>
          ) : complianceAuthority.items.length === 0 ? (
            <p
              data-testid="guideline-compliance-none"
              className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-600 dark:border-surface-700 dark:text-surface-300"
            >
              No guideline metric applies to this {entityType}. Adopted
              guidelines remain context-only here.
            </p>
          ) : (
            complianceAuthority.items.map((binding) => {
              const assessment =
                currentAssessmentByBinding.get(binding.bindingId) ?? null;
              const resultByMetric = new Map(
                (assessment?.metric_results ?? []).map((result) => [
                  result.metric_id,
                  result,
                ]),
              );
              return (
                <article
                  key={binding.bindingId}
                  data-testid={`guideline-compliance-${binding.bindingId}`}
                  className="space-y-2 rounded-xl border border-surface-200 p-3 dark:border-surface-700"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold text-surface-900 dark:text-white">
                      {binding.guidelineTitle}
                    </span>
                    <EnforcementBadge enforcement={binding.enforcement} />
                    <ComplianceStateChip assessment={assessment} />
                  </div>
                  <div className="flex flex-wrap items-start justify-start gap-x-6 gap-y-3 pt-1">
                    {binding.metrics.map((metric) => {
                      const result = resultByMetric.get(metric.metricId);
                      return (
                        <div
                          key={metric.metricId}
                          title={metric.description}
                          data-testid={`guideline-metric-${binding.bindingId}-${metric.metricId}`}
                          className="w-32 cursor-help"
                        >
                          {result ? (
                            <MetricScoreRing
                              label={metric.title}
                              value={result.score}
                              direction={semanticMetricDirection(
                                metric.direction,
                              )}
                              threshold={metric.effectiveThreshold}
                              testId={`guideline-metric-ring-${metric.metricId}`}
                            />
                          ) : (
                            <div className="flex min-w-0 flex-col items-center text-center">
                              <div
                                data-testid={`guideline-metric-ring-${metric.metricId}`}
                                data-status="neutral"
                                className="flex h-20 w-20 shrink-0 items-center justify-center rounded-full border-4 border-dashed border-surface-300 text-surface-400 dark:border-surface-600 dark:text-surface-500"
                              >
                                <span className="text-2xl font-bold leading-none">
                                  —
                                </span>
                              </div>
                              <p className="mt-2 text-xs font-semibold text-surface-700 dark:text-surface-200">
                                {metric.title}
                              </p>
                              <p className="mt-1 text-[10px] text-surface-500 dark:text-surface-400">
                                {metric.direction === 'minimum' ? 'Minimum' : 'Maximum'}{' '}
                                {metric.effectiveThreshold} · Not assessed
                              </p>
                            </div>
                          )}
                          {metric.overridden && (
                            <p className="mt-0.5 text-center text-[10px] text-surface-400 dark:text-surface-500">
                              Board override
                            </p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  {assessment && (
                    <p className="flex flex-wrap items-center gap-2 text-[11px] text-surface-500 dark:text-surface-400">
                      <CurrentnessBadge
                        currentness={assessment.currentness}
                      />
                      <span>
                        Confidence {assessment.confidence}
                        {binding.minimumConfidence !== null
                          ? ` (min ${binding.minimumConfidence})`
                          : ''}
                      </span>
                      <span>{formatTimestamp(assessment.recorded_at)}</span>
                    </p>
                  )}
                </article>
              );
            })
          )}
        </section>

        <details
          className="rounded-xl border border-surface-200 dark:border-surface-700"
          data-testid="policy-compliance-advanced"
        >
          <summary className="cursor-pointer select-none px-3 py-2 text-xs font-medium text-surface-600 hover:text-surface-900 dark:text-surface-300 dark:hover:text-white">
            Governance details — receipts, waivers, skips and history
          </summary>
          <div className="space-y-5 border-t border-surface-200 p-3 dark:border-surface-700">

        {!evaluationEnabled && (
          <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-200">
            {evaluationUnavailableReason
              ?? 'New assessments are unavailable for this lifecycle state.'}
            {' '}Existing immutable evidence remains visible.
          </p>
        )}

        <TransitionBindingSkips
          items={transitionBindings.items}
          error={transitionBindings.error ?? activeSkipResolution.error}
          subjectVersion={currentSubjectVersion}
          skipsReady={
            skips.loaded
            && !skips.loading
            && !skips.error
            && !skips.hasMore
            && activeSkipResolution.error === null
          }
          activeSkipByBinding={activeSkipResolution.items}
          canManage={canManageSkips}
          onCreate={(authority) => setSkipDialog({
            mode: 'create',
            authority,
          })}
          onRevoke={(skip) => setSkipDialog({
            mode: 'revoke',
            skip,
          })}
        />

        <section className="space-y-3" aria-busy={assessments.loading}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-sm font-semibold text-surface-800 dark:text-surface-100">
              Latest assessment state by binding
            </h4>
            {gateEvidenceReady && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                <CheckCircle2 size={13} aria-hidden="true" />
                All assessment receipt pages loaded; displayed receipts are
                admissible
              </span>
            )}
            {completeCurrentSet && !gateEvidenceReady && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                <AlertTriangle size={13} aria-hidden="true" />
                All assessment receipt pages loaded; displayed receipts are
                not gate-ready
              </span>
            )}
          </div>

          {assessments.loading && !assessments.loaded ? (
            <p
              role="status"
              className="rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
            >
              Loading semantic guideline assessments…
            </p>
          ) : assessments.error && assessments.items.length === 0 ? (
            <div
              role="alert"
              className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
            >
              <p>
                Assessment evidence could not be verified. No gate state is
                inferred.
              </p>
              <p className="mt-1">{assessments.error}</p>
            </div>
          ) : currentResolution.error ? (
            <div
              role="alert"
              className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
            >
              {currentResolution.error} The panel is fail-closed.
            </div>
          ) : currentResolution.items.length === 0 ? (
            <p
              className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400"
              data-testid="policy-compliance-empty"
            >
              No semantic assessment is available for a loaded guideline
              binding. An authorized agent must assess every applicable
              binding before a governed transition can proceed.
            </p>
          ) : (
            <div className="space-y-4">
              {currentResolution.items.map((assessment) => (
                <AssessmentCard
                  key={assessment.receipt_id}
                  assessment={assessment}
                  activeSkip={
                    activeSkipResolution.items.get(assessment.binding_id)
                    ?? null
                  }
                  canManageSkips={
                    canManageSkips
                    && skips.loaded
                    && !skips.error
                    && !skips.hasMore
                    && activeSkipResolution.error === null
                    && !transitionBindingIds.has(assessment.binding_id)
                  }
                  onCreateSkip={(item) => setSkipDialog({
                    mode: 'create',
                    authority: {
                      bindingId: item.binding_id,
                      guidelineId: item.guideline_id,
                      subjectVersion: item.subject_version,
                      source: 'assessment',
                    },
                  })}
                  onRevokeSkip={(item) => setSkipDialog({
                    mode: 'revoke',
                    skip: item,
                  })}
                />
              ))}
            </div>
          )}

          <CursorCollectionControls
            collectionLabel="semantic assessments"
            itemCount={assessments.items.length}
            hasMore={assessments.hasMore}
            loading={assessments.loading}
            error={assessments.error}
            restartRequired={assessments.restartRequired}
            onLoadMore={assessments.loadMore}
            onRetry={assessments.retry}
            onRestart={assessments.restart}
            testId="semantic-assessments-cursor"
          />
        </section>

        <CollapsibleEvidenceSection
          title="Assessment history"
          description="Append-only receipts across every loaded subject × binding evaluation."
          expanded={historyExpanded}
          onToggle={() => setHistoryExpanded((value) => !value)}
          testId="policy-compliance-history"
        >
          {assessments.items.length === 0 ? (
            <p className="text-xs text-surface-500 dark:text-surface-400">
              No assessment receipts have been loaded.
            </p>
          ) : (
            <ol className="space-y-2">
              {assessments.items.map((assessment) => (
                <li
                  key={assessment.receipt_id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-surface-200 p-3 text-xs dark:border-surface-700"
                >
                  <span className="text-surface-700 dark:text-surface-200">
                    {assessment.metric_count} metrics ·{' '}
                    {assessment.failed_metric_count} failed ·{' '}
                    {formatTimestamp(assessment.recorded_at)}
                  </span>
                  <span className="flex items-center gap-2">
                    <CurrentnessBadge currentness={assessment.currentness} />
                    <code className="text-[10px] text-surface-500">
                      {shortIdentity(assessment.receipt_id)}
                    </code>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </CollapsibleEvidenceSection>

        <CollapsibleEvidenceSection
          title="Pinpoint metric findings"
          description="Failed metric evidence anchored to an immutable assessment receipt."
          expanded={findingsExpanded}
          onToggle={() => setFindingsExpanded((value) => !value)}
          testId="policy-compliance-findings"
        >
          <Findings
            items={findings.items}
            canRequestWaiver={canRequestWaiver}
            onRequestWaiver={requestWaiver}
          />
          <CursorCollectionControls
            collectionLabel="semantic findings"
            itemCount={findings.items.length}
            hasMore={findings.hasMore}
            loading={findings.loading}
            error={findings.error}
            restartRequired={findings.restartRequired}
            onLoadMore={findings.loadMore}
            onRetry={findings.retry}
            onRestart={findings.restart}
            testId="semantic-findings-cursor"
          />
        </CollapsibleEvidenceSection>

        {canReadWaivers && (
          <CollapsibleEvidenceSection
            title="Metric waivers"
            description="Independently reviewed exceptions with live currentness."
            expanded={waiversExpanded}
            onToggle={() => setWaiversExpanded((value) => !value)}
            testId="semantic-waivers"
          >
            <Waivers items={waivers.items} />
            <CursorCollectionControls
              collectionLabel="metric waivers"
              itemCount={waivers.items.length}
              hasMore={waivers.hasMore}
              loading={waivers.loading}
              error={waivers.error}
              restartRequired={waivers.restartRequired}
              onLoadMore={waivers.loadMore}
              onRetry={waivers.retry}
              onRestart={waivers.restart}
              testId="semantic-waivers-cursor"
            />
          </CollapsibleEvidenceSection>
        )}

        {canReadSkips && (
          <CollapsibleEvidenceSection
            title="Human binding skips"
            description="Audited REST-only exceptions that agents cannot create or revoke."
            expanded={skipsExpanded}
            onToggle={() => setSkipsExpanded((value) => !value)}
            testId="semantic-skips"
          >
            <Skips
              items={skips.items}
              canManage={canManageSkips}
              onRevoke={(skip) => setSkipDialog({
                mode: 'revoke',
                skip,
              })}
            />
            <CursorCollectionControls
              collectionLabel="human binding skips"
              itemCount={skips.items.length}
              hasMore={skips.hasMore}
              loading={skips.loading}
              error={skips.error}
              restartRequired={skips.restartRequired}
              onLoadMore={skips.loadMore}
              onRetry={skips.retry}
              onRestart={skips.restart}
              testId="semantic-skips-cursor"
            />
          </CollapsibleEvidenceSection>
        )}

        <p className="flex items-start gap-1.5 rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-300">
          <AlertTriangle
            size={14}
            className="mt-0.5 shrink-0"
            aria-hidden="true"
          />
          Native workflow gates remain separate. Semantic guideline gates use
          only current, admissible receipts plus authoritative waivers or
          human-owned binding skips; unknown or partial evidence never enables
          a transition.
        </p>
          </div>
        </details>
      </div>

      {waiverFinding && !onRequestWaiver && (
        <WaiverRequestDialog
          boardId={boardId}
          finding={waiverFinding}
          onClose={() => setWaiverFinding(null)}
          onCompleted={() => {
            setWaiverFinding(null);
            refreshAll();
          }}
        />
      )}
      {skipDialog && (
        <SkipDialog
          boardId={boardId}
          entityType={entityType}
          subjectId={subjectId}
          state={skipDialog}
          onClose={() => setSkipDialog(null)}
          onCompleted={() => {
            setSkipDialog(null);
            refreshAll();
          }}
        />
      )}
    </>
  );
}

export default PolicyCompliancePanel;
