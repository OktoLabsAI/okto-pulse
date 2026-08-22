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
import {
  PreviousResultsSection,
  ValidationCycleStatusBadge,
} from '@/components/validation-cycle/ValidationCyclePrimitives';
import { useOpaqueCursorCollection } from '@/hooks/useOpaqueCursorCollection';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import { useDashboardApi } from '@/services/api';
import { recordPolicyComplianceRender } from '@/services/policy-compliance-telemetry';
import type {
  PolicyComplianceLifecycleBindingStatus,
  PolicyComplianceLifecycleDetails,
  PolicyComplianceLifecycleMetricOutcome,
} from '@/types';
import type {
  GuidelineMetricDirection,
  NonEmptyArray,
  PolicyEntityType,
  SemanticAssessmentDetail,
  SemanticCurrentAssessmentResponse,
  SemanticEvidenceRef,
  SemanticFindingDetail,
  SemanticSkipDetail,
  SemanticWaiverDetail,
} from '@/types/policy-governance';

import {
  ActionablePinpoint,
  PolicyComplianceReadOnlyActions,
} from './ActionablePinpoint';
import {
  parseCurrentSemanticAssessmentResponse,
  parseSemanticAssessmentDetail,
  parseCreatedSemanticSkipResponse,
  parseSemanticDetailPage,
  parseSemanticFindingDetail,
  parseSemanticSkipDetail,
  parseSemanticWaiverDetail,
  parseRequestedSemanticWaiverResponse,
  parseRevokedSemanticSkipResponse,
  resolveSemanticPolicyViewModel,
  semanticPolicyRenderTelemetry,
  semanticMetricDirection,
  type SemanticAnchorResolution,
  type SemanticPolicyUiState,
  type SemanticPolicyResolverOptions,
  type SemanticPolicyViewModel,
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
  /** Human validation edition used only by the opt-in Spec lifecycle view. */
  subjectEdition?: number;
  /** Preserve the established technical UI unless a Spec explicitly opts in. */
  presentationMode?: 'legacy' | 'lifecycle-edition';
  /** Suppresses the repeated title inside the unified Spec workspace. */
  embedded?: boolean;
  /**
   * Exact, already envelope-validated lifecycle authority for this subject.
   * Binding decisions allow a human skip before an admissible receipt exists.
   */
  transitionPreview?: PolicyTransitionPreviewLoadState;
  /**
   * Immutable human projection frozen for the rendered Spec edition.
   * Undefined means it is still loading; null means it could not be verified.
   */
  lifecycleSnapshot?: PolicyComplianceLifecycleDetails | null;
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
  /** Current subject authorization resolver; absent authority fails closed. */
  resolveSemanticAnchor?: (
    anchor: Parameters<NonNullable<SemanticPolicyResolverOptions['resolveAnchor']>>[0],
  ) => SemanticAnchorResolution;
  /** Optional host navigation for a target returned by the authorized resolver. */
  onNavigateSemanticAnchor?: (target: string) => void;
  /** Optional host guidance action; the panel also provides safe inline guidance. */
  onOpenReassessmentGuidance?: () => void;
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
  descriptionTruncated: boolean;
  rubricTruncated: boolean;
  assessmentOutcome?: PolicyComplianceLifecycleMetricOutcome;
  direction: GuidelineMetricDirection;
  effectiveThreshold: number;
  overridden: boolean;
}

interface BindingComplianceAuthority {
  bindingId: string;
  guidelineId: string;
  revisionId: string;
  guidelineTitle: string;
  enforcement: 'advisory' | 'blocking';
  minimumConfidence: number | null;
  lifecycleStatus?: PolicyComplianceLifecycleBindingStatus;
  failedMetricCount?: number;
  waivedMetricCount?: number;
  unwaivedFailedMetricCount?: number;
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

interface CurrentSemanticAssessmentState {
  scope: string;
  status: 'idle' | 'loading' | 'ready' | 'error';
  responses: Record<string, SemanticCurrentAssessmentResponse>;
  missingBindingIds: string[];
  message: string | null;
}

function primarySemanticState(
  view: SemanticPolicyViewModel,
): SemanticPolicyUiState {
  if (view.currentness === 'stale') return 'stale';
  if (view.contractVersion === 'v1') return 'legacy';
  const priority: SemanticPolicyUiState[] = [
    'fail',
    'waived_fail_finding',
    'non_blocking_warning',
    'positive_evidence',
  ];
  return priority.find((state) => view.uiStates.includes(state))
    ?? 'no_visible_pinpoints';
}

function SemanticStateChip({ state }: { state: SemanticPolicyUiState }) {
  const tone = state === 'fail'
    ? 'bg-red-100 text-red-700 dark:bg-red-400/15 dark:text-red-200'
    : state === 'positive_evidence'
      ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-400/15 dark:text-emerald-200'
      : state === 'waived_fail_finding'
        ? 'bg-violet-100 text-violet-700 dark:bg-violet-400/15 dark:text-violet-200'
        : state === 'stale' || state === 'non_blocking_warning'
          ? 'bg-amber-100 text-amber-700 dark:bg-amber-400/15 dark:text-amber-200'
          : 'bg-surface-100 text-surface-600 dark:bg-surface-700/60 dark:text-surface-300';
  const label = state === 'positive_evidence'
    ? 'Passed'
    : state === 'non_blocking_warning'
      ? 'Warning · Passed'
      : state === 'waived_fail_finding'
        ? 'Waiver active'
        : state === 'legacy'
          ? 'V1 · Read-only'
          : state.split('_').join(' ');
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${tone}`}>
      {state === 'fail' || state === 'stale'
        ? <AlertTriangle size={11} aria-hidden="true" />
        : <CheckCircle2 size={11} aria-hidden="true" />}
      {label}
    </span>
  );
}

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

function LifecycleComplianceStateChip({
  status,
  enforcement,
}: {
  status: PolicyComplianceLifecycleBindingStatus;
  enforcement: 'advisory' | 'blocking';
}) {
  const label = status === 'passed'
    ? 'Passed'
    : status === 'waived'
      ? 'Waived'
      : status === 'skipped'
        ? 'Skipped'
        : status === 'pending'
          ? enforcement === 'advisory'
            ? 'Advisory pending'
            : 'Not assessed'
          : status === 'inconsistent'
            ? 'Unavailable'
            : enforcement === 'advisory'
              ? 'Advisory finding'
              : 'Failed';
  const tone = status === 'passed'
    ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-400/15 dark:text-emerald-200'
    : status === 'waived'
      ? 'bg-violet-100 text-violet-700 dark:bg-violet-400/15 dark:text-violet-200'
      : status === 'failed' && enforcement === 'blocking'
        ? 'bg-red-100 text-red-700 dark:bg-red-400/15 dark:text-red-200'
        : status === 'failed' || status === 'inconsistent'
          ? 'bg-amber-100 text-amber-700 dark:bg-amber-400/15 dark:text-amber-200'
          : 'bg-surface-100 text-surface-600 dark:bg-surface-700/60 dark:text-surface-300';
  return (
    <span
      data-testid={`lifecycle-policy-status-${status}`}
      className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${tone}`}
    >
      {label}
    </span>
  );
}

function LifecycleMetricOutcomeChip({
  outcome,
  metricId,
  enforcement,
}: {
  outcome: PolicyComplianceLifecycleMetricOutcome;
  metricId: string;
  enforcement: 'advisory' | 'blocking';
}) {
  const label = outcome === 'passed'
    ? 'Passed'
    : outcome === 'waived'
      ? 'Waiver active'
      : outcome === 'failed'
        ? enforcement === 'advisory' ? 'Advisory finding' : 'Finding remains'
        : 'Not assessed';
  const tone = outcome === 'passed'
    ? 'text-emerald-700 dark:text-emerald-300'
    : outcome === 'waived'
      ? 'text-violet-700 dark:text-violet-300'
      : outcome === 'failed'
        ? 'text-amber-700 dark:text-amber-300'
        : 'text-surface-500 dark:text-surface-400';
  return (
    <p
      className={`mt-1 text-center text-[10px] font-semibold ${tone}`}
      data-testid={`lifecycle-policy-metric-outcome-${metricId}`}
    >
      {label}
    </p>
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
  subjectEdition,
  presentationMode = 'legacy',
  embedded = false,
  transitionPreview,
  lifecycleSnapshot,
  evaluationEnabled = true,
  evaluationUnavailableReason,
  onRequestWaiver,
  onRefreshed,
  refreshKey = 0,
  resolveSemanticAnchor,
  onNavigateSemanticAnchor,
  onOpenReassessmentGuidance,
}: PolicyCompliancePanelProps) {
  const lifecycleMode = presentationMode === 'lifecycle-edition';
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [findingsExpanded, setFindingsExpanded] = useState(false);
  const [waiversExpanded, setWaiversExpanded] = useState(false);
  const [skipsExpanded, setSkipsExpanded] = useState(false);
  const [advancedExpanded, setAdvancedExpanded] = useState(false);
  const [localRefresh, setLocalRefresh] = useState(0);
  const [guidanceVisible, setGuidanceVisible] = useState(false);
  const advancedDetailsRef = useRef<HTMLDetailsElement | null>(null);
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

  const subjectExpectation = useMemo<SemanticSubjectExpectation>(
    () => ({ boardId, entityType, subjectId }),
    [boardId, entityType, subjectId],
  );
  const currentExpectation = useMemo<SemanticSubjectExpectation>(
    () => ({
      boardId,
      entityType,
      subjectId,
      validationEdition: lifecycleMode ? subjectEdition : undefined,
    }),
    [boardId, entityType, lifecycleMode, subjectEdition, subjectId],
  );
  const resetScope = JSON.stringify([
    boardId,
    entityType,
    subjectId,
    refreshKey,
    localRefresh,
    lifecycleMode,
    subjectEdition,
  ]);
  const evaluatedAt = useMemo(
    () => new Date().toISOString(),
    [resetScope],
  );

  const dashboardApi = useDashboardApi();
  const [complianceAuthority, setComplianceAuthority] =
    useState<ComplianceAuthorityState>({ status: 'loading', items: [] });
  const semanticScope = JSON.stringify([
    boardId,
    entityType,
    subjectId,
    lifecycleMode,
    lifecycleMode ? subjectEdition : null,
  ]);
  const [currentSemantic, setCurrentSemantic] =
    useState<CurrentSemanticAssessmentState>({
      scope: semanticScope,
      status: 'idle',
      responses: {},
      missingBindingIds: [],
      message: null,
    });

  useEffect(() => {
    if (!canRead) return undefined;
    if (lifecycleMode) {
      if (lifecycleSnapshot === undefined) {
        setComplianceAuthority({ status: 'loading', items: [] });
      } else if (lifecycleSnapshot === null) {
        setComplianceAuthority({
          status: 'error',
          items: [],
          message: 'The frozen policy scope for this edition could not be verified.',
        });
      } else {
        setComplianceAuthority({
          status: 'ready',
          items: lifecycleSnapshot.applicable_bindings.map((binding) => ({
            bindingId: binding.binding_id,
            guidelineId: binding.guideline_id,
            revisionId: binding.revision_id,
            guidelineTitle: binding.title,
            enforcement: binding.enforcement,
            minimumConfidence: binding.minimum_confidence,
            lifecycleStatus: binding.status,
            failedMetricCount: binding.failed_metric_count,
            waivedMetricCount: binding.waived_metric_count,
            unwaivedFailedMetricCount: binding.unwaived_failed_metric_count,
            metrics: binding.metrics.map((metric) => ({
              metricId: metric.metric_id,
              code: metric.code,
              title: metric.title,
              description: metric.description,
              descriptionTruncated: metric.description_truncated,
              rubricTruncated: metric.evaluation_rubric_truncated,
              assessmentOutcome: metric.assessment_outcome,
              direction: metric.direction,
              effectiveThreshold: metric.effective_threshold,
              overridden: metric.threshold_source === 'override',
            })),
          })),
        });
      }
      return undefined;
    }
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
              descriptionTruncated: false,
              rubricTruncated: false,
              assessmentOutcome: undefined,
              direction: metric.direction,
              effectiveThreshold:
                overrides[metric.code] ?? metric.default_threshold,
              overridden: overrides[metric.code] !== undefined,
            }));
          if (metrics.length > 0) {
            items.push({
              bindingId: entry.binding_id,
              guidelineId: entry.guideline.id,
              revisionId,
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
  }, [canRead, lifecycleMode, lifecycleSnapshot, resetScope]);

  useEffect(() => {
    if (!canRead || complianceAuthority.status !== 'ready') return undefined;
    const controller = new AbortController();
    let cancelled = false;
    setCurrentSemantic((previous) => ({
      scope: semanticScope,
      status: 'loading',
      responses: previous.scope === semanticScope ? previous.responses : {},
      missingBindingIds: [],
      message: null,
    }));
    void (async () => {
      const responses: Record<string, SemanticCurrentAssessmentResponse> = {};
      const missingBindingIds: string[] = [];
      const errors: string[] = [];
      await Promise.all(complianceAuthority.items.map(async (binding) => {
        if (
          lifecycleMode
          && (
            binding.lifecycleStatus === 'skipped'
            || binding.lifecycleStatus === 'inconsistent'
          )
        ) {
          missingBindingIds.push(binding.bindingId);
          return;
        }
        try {
          const response = await api.getCurrentSemanticGuidelineAssessment(
            boardId,
            entityType,
            subjectId,
            binding.bindingId,
            'detail',
            controller.signal,
            lifecycleMode ? subjectEdition : undefined,
          );
          const parsed = parseCurrentSemanticAssessmentResponse(
            response,
            currentExpectation,
          );
          if (
            parsed.assessment.binding_id !== binding.bindingId
            || parsed.assessment.guideline_id !== binding.guidelineId
            || parsed.assessment.guideline_revision_id !== binding.revisionId
          ) {
            throw new Error(
              'Semantic assessment does not match the authoritative guideline binding.',
            );
          }
          responses[binding.bindingId] = parsed;
        } catch (caught) {
          if (
            caught instanceof PolicyGovernanceApiError
            && (caught.status === 404 || caught.kind === 'not_found')
          ) {
            missingBindingIds.push(binding.bindingId);
            return;
          }
          errors.push(semanticError(caught).message);
        }
      }));
      if (cancelled) return;
      setCurrentSemantic((previous) => ({
        scope: semanticScope,
        status: errors.length > 0 ? 'error' : 'ready',
        responses: errors.length > 0 && previous.scope === semanticScope
          ? { ...previous.responses, ...responses }
          : responses,
        missingBindingIds,
        message: errors.length > 0
          ? 'Assessment could not be refreshed. The last valid evidence remains visible.'
          : null,
      }));
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
    // The authority reload caused by resetScope is the refresh trigger. Keeping
    // resetScope here as well would race one request against stale authority and
    // then issue a second request when the refreshed authority arrives.
    // The api hook identity stays excluded for request-loop protection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canRead, complianceAuthority, currentExpectation, lifecycleMode, semanticScope, subjectEdition]);

  useEffect(() => {
    setHistoryExpanded(false);
    setFindingsExpanded(false);
    setWaiversExpanded(false);
    setSkipsExpanded(false);
    setAdvancedExpanded(false);
    setGuidanceVisible(false);
    setWaiverFinding(null);
    setSkipDialog(null);
  }, [semanticScope]);

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
    (item) => parseSemanticAssessmentDetail(item, subjectExpectation),
    PAGE_SIZE,
  ), [api, boardId, entityType, subjectExpectation, subjectId]);

  const assessments = useOpaqueCursorCollection({
    enabled: canRead && (!lifecycleMode || historyExpanded || advancedExpanded),
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
    (item) => parseSemanticFindingDetail(item, subjectExpectation),
    PAGE_SIZE,
  ), [api, boardId, entityType, subjectExpectation, subjectId]);

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
    (item) => parseSemanticWaiverDetail(item, subjectExpectation),
    PAGE_SIZE,
  ), [
    api,
    boardId,
    entityType,
    evaluatedAt,
    subjectExpectation,
    subjectId,
  ]);

  const waivers = useOpaqueCursorCollection({
    enabled: canReadWaivers && (!lifecycleMode || advancedExpanded),
    resetKey: `${resetScope}:waivers:${evaluatedAt}`,
    loadPage: loadWaiverPage,
    getItemKey: (item: SemanticWaiverDetail) => item.waiver_id,
    classifyError: semanticError,
  });

  const currentSemanticViews = useMemo(() => {
    const waivedByBinding = new Map<string, Set<string>>();
    for (const waiver of waivers.items) {
      if (waiver.status !== 'approved' || waiver.currentness !== 'current') {
        continue;
      }
      const codes = waivedByBinding.get(waiver.binding_id) ?? new Set<string>();
      codes.add(waiver.metric_code);
      waivedByBinding.set(waiver.binding_id, codes);
    }
    const views = new Map<string, SemanticPolicyViewModel>();
    let error: string | null = null;
    for (const [bindingId, response] of Object.entries(
      currentSemantic.responses,
    )) {
      try {
        views.set(bindingId, resolveSemanticPolicyViewModel(response, {
          resolveAnchor: resolveSemanticAnchor,
          canViewTechnicalDetails: !lifecycleMode,
          waivedMetricCodes: waivedByBinding.get(bindingId),
        }));
      } catch (caught) {
        error = semanticError(caught).message;
      }
    }
    return { views, error };
  }, [currentSemantic.responses, lifecycleMode, resolveSemanticAnchor, waivers.items]);

  useEffect(() => {
    if (currentSemantic.status === 'loading') {
      recordPolicyComplianceRender(
        semanticPolicyRenderTelemetry('loading', 'none'),
      );
      return;
    }
    if (currentSemantic.status === 'error' || currentSemanticViews.error) {
      recordPolicyComplianceRender(
        semanticPolicyRenderTelemetry('recoverable_transport_error', 'none'),
      );
    }
    for (const view of currentSemanticViews.views.values()) {
      recordPolicyComplianceRender(semanticPolicyRenderTelemetry(
        primarySemanticState(view),
        view.contractVersion,
      ));
    }
    if (currentSemantic.status === 'ready') {
      currentSemantic.missingBindingIds.forEach(() => {
        recordPolicyComplianceRender(
          semanticPolicyRenderTelemetry('no_assessment', 'none'),
        );
      });
    }
  }, [currentSemantic, currentSemanticViews]);

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
    (item) => parseSemanticSkipDetail(item, subjectExpectation),
    PAGE_SIZE,
  ), [api, boardId, entityType, subjectExpectation, subjectId]);

  const skips = useOpaqueCursorCollection({
    enabled: canReadSkips && (!lifecycleMode || advancedExpanded),
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
  const currentLifecycleReceiptIds = new Set(
    Object.values(currentSemantic.responses).map(
      (response) => response.assessment.receipt_id,
    ),
  );
  const previousLifecycleAssessments = lifecycleMode
    ? assessments.items.filter(
        (assessment) => !currentLifecycleReceiptIds.has(assessment.receipt_id),
      )
    : [];
  const guidelineTitleByBinding = new Map(
    complianceAuthority.items.map((binding) => [
      binding.bindingId,
      binding.guidelineTitle,
    ]),
  );

  return (
    <>
      <div className="space-y-5" data-testid="policy-compliance-panel">
        {!(lifecycleMode && embedded) && (
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-2 text-base font-semibold text-surface-900 dark:text-white">
              <ShieldCheck
                size={18}
                className="text-violet-600 dark:text-violet-300"
                aria-hidden="true"
              />
              {lifecycleMode ? 'Policy compliance' : 'Semantic guideline assessments'}
            </h3>
            <p className="mt-1 max-w-3xl text-xs text-surface-500 dark:text-surface-400">
              {lifecycleMode
                ? `An external agent evaluates adopted guidelines for ${subjectEdition == null ? 'the current edition' : `Edition ${subjectEdition}`}; Pulse verifies the submitted result.`
                : 'Metric scores are recorded by agents against the adopted guideline revisions; Pulse verifies and seals them.'}
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
              disabled={assessments.loading || currentSemantic.status === 'loading'}
              className="inline-flex min-h-8 items-center gap-1 rounded-lg border border-surface-300 bg-white px-2.5 py-1 text-xs text-surface-700 hover:bg-surface-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
            >
              <RefreshCw
                size={13}
                className={
                  assessments.loading || currentSemantic.status === 'loading'
                    ? 'animate-spin'
                    : ''
                }
                aria-hidden="true"
              />
              Refresh
            </button>
          </div>
        </header>
        )}

        <section
          className="space-y-3"
          data-testid="guideline-compliance-summary"
        >
          {lifecycleMode
            && lifecycleSnapshot
            && lifecycleSnapshot.counts.scope_inconsistent > 0 && (
            <p
              role="alert"
              className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200"
              data-testid="policy-compliance-scope-inconsistent"
            >
              {lifecycleSnapshot.counts.scope_inconsistent}{' '}
              frozen policy scope {lifecycleSnapshot.counts.scope_inconsistent === 1 ? 'item is' : 'items are'} unavailable. Verified applicable policies are shown below; no current board policy was substituted.
            </p>
          )}
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
              {lifecycleMode
                ? lifecycleSnapshot && lifecycleSnapshot.counts.scope_inconsistent > 0
                  ? <>No applicable policy details could be verified for {subjectEdition == null ? 'this edition' : `Edition ${subjectEdition}`}.</>
                  : <>
                      No policies apply to {subjectEdition == null ? 'this edition' : `Edition ${subjectEdition}`}.
                      {' '}{lifecycleSnapshot?.counts.context_only ?? 0}{' '}
                      adopted {lifecycleSnapshot?.counts.context_only === 1 ? 'guideline is' : 'guidelines are'} context-only for this Spec.
                    </>
                : <>No guideline metric applies to this {entityType}. Adopted guidelines remain context-only here.</>}
            </p>
          ) : (
            <>
              {currentSemantic.status === 'error' && (
                <div role="alert" aria-live="assertive" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200">
                  {currentSemantic.message}
                  <div className="mt-2">
                    <PolicyComplianceReadOnlyActions onRetry={refreshAll} />
                  </div>
                </div>
              )}
              {currentSemanticViews.error && (
                <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200">
                  Semantic assessment evidence could not be projected safely.
                </p>
              )}
              {complianceAuthority.items.map((binding) => {
                const view = currentSemanticViews.views.get(binding.bindingId);
                const state = view ? primarySemanticState(view) : null;
                const resultByCode = new Map(
                  (view?.metrics ?? []).map((result) => [
                    result.metricCode,
                    result,
                  ]),
                );
                const metricTitleByCode = new Map(
                  binding.metrics.map((metric) => [metric.code, metric.title]),
                );
                const isLoading = currentSemantic.status === 'loading' && !view;
                const noAssessment = currentSemantic.status === 'ready'
                  && currentSemantic.missingBindingIds.includes(binding.bindingId);
                const lifecycleStatus = binding.lifecycleStatus;
                const pinpoints = view?.metrics.flatMap((metric) =>
                  metric.pinpoints.map((pinpoint) => ({
                    metricLabel: metricTitleByCode.get(metric.metricCode)
                      ?? metric.metricCode,
                    metricState: metric.uiState,
                    metricOutcome: metric.outcome,
                    lifecycleMetricOutcome: binding.metrics.find(
                      (authority) => authority.code === metric.metricCode,
                    )?.assessmentOutcome,
                    pinpoint,
                  }))
                ) ?? [];
                const lifecycleReassessmentNeeded = lifecycleMode
                  && noAssessment
                  && lifecycleStatus === 'pending';
                const failedMetricCount = binding.failedMetricCount ?? 0;
                const waivedMetricCount = binding.waivedMetricCount ?? 0;
                const unwaivedFailedMetricCount =
                  binding.unwaivedFailedMetricCount ?? 0;
                return (
                <article
                  key={binding.bindingId}
                  data-testid={`guideline-compliance-${binding.bindingId}`}
                  className="space-y-3 rounded-xl border border-surface-200 p-3 dark:border-surface-700"
                  aria-busy={isLoading}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold text-surface-900 dark:text-white">
                      {binding.guidelineTitle}
                    </span>
                    <EnforcementBadge enforcement={binding.enforcement} />
                    {lifecycleMode && lifecycleStatus
                      ? (
                          <LifecycleComplianceStateChip
                            status={lifecycleStatus}
                            enforcement={binding.enforcement}
                          />
                        )
                      : state
                        ? <SemanticStateChip state={state} />
                        : <ComplianceStateChip assessment={null} />}
                    {view && !lifecycleMode && (
                      <span className="rounded-full bg-surface-100 px-2 py-0.5 text-[10px] font-semibold uppercase text-surface-600 dark:bg-surface-800 dark:text-surface-300">
                        {view.contractVersion}
                      </span>
                    )}
                  </div>
                  {isLoading && (
                    <div role="status" className="space-y-2 rounded-lg border border-surface-200 p-3 text-xs text-surface-500 dark:border-surface-700 dark:text-surface-400">
                      <span className="block h-3 w-36 animate-pulse rounded bg-surface-200 dark:bg-surface-700" />
                      Loading assessment evidence…
                    </div>
                  )}
                  {noAssessment && lifecycleStatus === 'skipped' && (
                    <div className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-800/40 dark:text-surface-300" data-testid="policy-compliance-skipped">
                      <p className="font-semibold">Assessment skipped for this edition</p>
                      <p className="mt-1">An authorized human resolved this applicable policy with an audited skip.</p>
                    </div>
                  )}
                  {noAssessment && lifecycleStatus === 'inconsistent' && (
                    <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200" data-testid="policy-compliance-inconsistent">
                      <p className="font-semibold">Policy result unavailable</p>
                      <p className="mt-1">The frozen policy scope could not be reconciled safely for this binding.</p>
                    </div>
                  )}
                  {noAssessment && lifecycleStatus !== 'skipped' && lifecycleStatus !== 'inconsistent' && (
                    <div className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-600 dark:border-surface-700 dark:text-surface-300" data-testid="policy-compliance-no-assessment">
                      <p className="font-semibold">
                        {lifecycleMode && lifecycleStatus !== 'pending'
                          ? 'Assessment detail unavailable'
                          : 'No assessment recorded'}
                      </p>
                      <p className="mt-1">
                        {lifecycleMode && lifecycleStatus !== 'pending'
                          ? 'The frozen summary remains authoritative, but its human-readable evidence could not be loaded.'
                          : lifecycleMode && binding.enforcement === 'advisory'
                            ? <>This pending advisory assessment does not block validation. Ask an independent agent to assess the current edition; scores cannot be entered here.</>
                            : <>Ask an independent agent to assess the current {lifecycleMode ? 'edition' : 'version'}. Scores cannot be entered here.</>}
                      </p>
                    </div>
                  )}
                  {lifecycleMode && lifecycleStatus === 'waived' && (
                    <p className="rounded-lg border border-violet-200 bg-violet-50 p-2 text-xs text-violet-800 dark:border-violet-800 dark:bg-violet-950/30 dark:text-violet-200" data-testid="policy-compliance-waived">
                      All {failedMetricCount}{' '}
                      failed metric {failedMetricCount === 1 ? 'finding is' : 'findings are'} covered by {failedMetricCount === 1 ? 'an approved waiver' : 'approved waivers'} for this edition.
                    </p>
                  )}
                  {lifecycleMode
                    && lifecycleStatus === 'failed'
                    && waivedMetricCount > 0 && (
                    <p className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200" data-testid="policy-compliance-partial-waiver">
                      {waivedMetricCount}{' '}
                      failed metric {waivedMetricCount === 1 ? 'finding is' : 'findings are'} covered by {waivedMetricCount === 1 ? 'an approved waiver' : 'approved waivers'};{' '}
                      {unwaivedFailedMetricCount}{' '}
                      {unwaivedFailedMetricCount === 1 ? 'finding remains' : 'findings remain'} unresolved.
                    </p>
                  )}
                  {lifecycleMode && lifecycleStatus === 'failed' && binding.enforcement === 'advisory' && (
                    <p className="rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200" data-testid="policy-compliance-advisory-note">
                      This advisory finding does not block validation.
                    </p>
                  )}
                  <div className="flex flex-wrap items-start justify-start gap-x-6 gap-y-3 pt-1">
                    {view && (
                      <div className="w-32" data-testid={`guideline-confidence-${binding.bindingId}`}>
                        <MetricScoreRing
                          label="Confidence"
                          value={view.confidence}
                          direction="higher-is-better"
                          threshold={binding.minimumConfidence}
                          testId={`guideline-confidence-ring-${binding.bindingId}`}
                        />
                      </div>
                    )}
                    {binding.metrics.map((metric) => {
                      const result = resultByCode.get(metric.code);
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
                              direction={semanticMetricDirection(result.direction)}
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
                          {lifecycleMode && metric.assessmentOutcome && (
                            <LifecycleMetricOutcomeChip
                              outcome={metric.assessmentOutcome}
                              metricId={metric.metricId}
                              enforcement={binding.enforcement}
                            />
                          )}
                          {(metric.descriptionTruncated || metric.rubricTruncated) && (
                            <p
                              className="mt-1 text-center text-[10px] font-medium text-amber-700 dark:text-amber-300"
                              data-testid={`guideline-metric-truncated-${metric.metricId}`}
                              title="The frozen policy text was shortened for this view."
                            >
                              Policy text excerpt truncated
                            </p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  {view && pinpoints.length === 0 && (
                    <p className="rounded-lg border border-dashed border-surface-300 p-3 text-xs text-surface-600 dark:border-surface-700 dark:text-surface-300" data-testid="policy-compliance-no-visible-pinpoints">
                      Assessment has no visible pinpoints.
                    </p>
                  )}
                  {pinpoints.length > 0 && (
                    <section className="space-y-2" aria-label={`${binding.guidelineTitle} pinpoints`}>
                      <h4 className="text-xs font-semibold text-surface-800 dark:text-surface-100">Actionable pinpoints</h4>
                      <div className="space-y-2">
                        {pinpoints.map(({
                          metricLabel,
                          metricState,
                          metricOutcome,
                          lifecycleMetricOutcome,
                          pinpoint,
                        }, index) => (
                          <ActionablePinpoint
                            key={`${pinpoint.contractVersion}:${index}:${pinpoint.title}`}
                            metricLabel={metricLabel}
                            pinpoint={pinpoint}
                            policyState={pinpoint.state === 'removed'
                              ? 'removed'
                              : pinpoint.state === 'inaccessible'
                                ? 'inaccessible'
                                : lifecycleMode
                                  ? lifecycleMetricOutcome === 'waived'
                                    ? 'waived_fail_finding'
                                    : lifecycleMetricOutcome === 'failed'
                                      ? binding.enforcement === 'advisory'
                                        ? 'non_blocking_warning'
                                        : 'fail'
                                      : lifecycleMetricOutcome === 'passed'
                                        ? 'positive_evidence'
                                        : metricOutcome === 'fail'
                                          ? binding.enforcement === 'advisory'
                                            ? 'non_blocking_warning'
                                            : 'fail'
                                          : 'positive_evidence'
                                  : metricState}
                            onNavigate={onNavigateSemanticAnchor}
                          />
                        ))}
                      </div>
                    </section>
                  )}
                  {view && (
                    <p className="flex flex-wrap items-center gap-2 text-[11px] text-surface-500 dark:text-surface-400">
                      {!lifecycleMode && <CurrentnessBadge currentness={view.currentness} />}
                      <span>
                        Confidence {view.confidence}
                        {binding.minimumConfidence !== null
                          ? ` (min ${binding.minimumConfidence})`
                          : ''}
                      </span>
                      <span>{formatTimestamp(view.recordedAt)}</span>
                    </p>
                  )}
                  <PolicyComplianceReadOnlyActions
                    onViewHistory={() => {
                      if (!lifecycleMode && advancedDetailsRef.current) {
                        advancedDetailsRef.current.open = true;
                      }
                      setHistoryExpanded(true);
                    }}
                    onViewReassessmentGuidance={
                      (!lifecycleMode && (state === 'stale' || noAssessment))
                      || lifecycleReassessmentNeeded
                        ? () => {
                            setGuidanceVisible(true);
                            onOpenReassessmentGuidance?.();
                          }
                        : undefined
                    }
                  />
                  {guidanceVisible && (
                    (!lifecycleMode && (state === 'stale' || noAssessment))
                    || lifecycleReassessmentNeeded
                  ) && (
                    <p role="status" aria-live="polite" className="rounded-lg bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
                      Reassessment is performed by an independent agent for the current {lifecycleMode ? 'edition' : 'subject and binding fences'}; this browser never accepts a manual score or executes cognition.
                    </p>
                  )}
                </article>
                );
              })}
            </>
          )}
        </section>

        {lifecycleMode && (
          <PreviousResultsSection
            expanded={historyExpanded}
            onToggle={() => setHistoryExpanded((value) => !value)}
            count={assessments.loaded && !assessments.hasMore
              ? previousLifecycleAssessments.length
              : undefined}
            description="Earlier policy evaluations from this and prior editions."
            testId="policy-compliance-previous-results"
          >
            {assessments.loading && !assessments.loaded ? (
              <p role="status" className="text-xs text-surface-500 dark:text-surface-400">
                Loading previous results…
              </p>
            ) : previousLifecycleAssessments.length === 0 ? (
              <p className="text-xs text-surface-500 dark:text-surface-400">
                No previous policy results are available.
              </p>
            ) : (
              <ol className="space-y-2">
                {previousLifecycleAssessments.map((assessment) => (
                  <li
                    key={assessment.receipt_id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-surface-200 bg-surface-50/70 p-3 text-xs dark:border-surface-700 dark:bg-surface-800/40"
                  >
                    <span>
                      <span className="font-semibold text-surface-800 dark:text-surface-100">
                        {assessment.validation_edition == null
                          ? 'Legacy'
                          : `Edition ${assessment.validation_edition}`}
                      </span>
                      <span className="ml-2 text-surface-500 dark:text-surface-400">
                        {guidelineTitleByBinding.get(assessment.binding_id)
                          ?? 'Policy evaluation'}{' '}
                        · {formatTimestamp(assessment.recorded_at)}
                      </span>
                    </span>
                    <ValidationCycleStatusBadge
                      state={assessment.failed_metric_count === 0
                        ? 'passed'
                        : 'needs_attention'}
                    />
                  </li>
                ))}
              </ol>
            )}
            <CursorCollectionControls
              collectionLabel="previous policy results"
              itemCount={assessments.items.length}
              hasMore={assessments.hasMore}
              loading={assessments.loading}
              error={assessments.error}
              restartRequired={assessments.restartRequired}
              onLoadMore={assessments.loadMore}
              onRetry={assessments.retry}
              onRestart={assessments.restart}
              testId="policy-previous-results-cursor"
            />
          </PreviousResultsSection>
        )}

        <details
          ref={advancedDetailsRef}
          onToggle={(event) => setAdvancedExpanded(event.currentTarget.open)}
          className="rounded-xl border border-surface-200 dark:border-surface-700"
          data-testid="policy-compliance-advanced"
        >
          <summary className="cursor-pointer select-none rounded-xl px-3 py-2 text-xs font-medium text-surface-600 hover:text-surface-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2 dark:text-surface-300 dark:hover:text-white dark:focus-visible:ring-offset-surface-900">
            {lifecycleMode
              ? 'Technical audit'
              : 'Governance details — receipts, waivers, skips and history'}
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
              {lifecycleMode
                ? 'Loaded assessment audit by binding'
                : 'Latest assessment state by binding'}
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

        {!lifecycleMode && <CollapsibleEvidenceSection
          title={lifecycleMode ? 'Previous results' : 'Assessment history'}
          description={lifecycleMode
            ? 'Earlier evaluations remain available for reference.'
            : 'Append-only receipts across every loaded subject × binding evaluation.'}
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
                  {!lifecycleMode && (
                    <span className="flex items-center gap-2">
                      <CurrentnessBadge currentness={assessment.currentness} />
                      <code className="text-[10px] text-surface-500">
                        {shortIdentity(assessment.receipt_id)}
                      </code>
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </CollapsibleEvidenceSection>}

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
