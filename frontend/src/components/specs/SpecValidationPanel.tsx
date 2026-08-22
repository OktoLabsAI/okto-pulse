import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  CheckCircle2,
  FileCheck2,
  RefreshCw,
} from 'lucide-react';

import {
  PolicyCompliancePanel,
  type PolicyTransitionRejection,
  type PolicyTransitionPreviewLoadState,
} from '@/components/policy-compliance';
import {
  parsePolicyComplianceLifecycleDetails,
} from '@/components/policy-compliance/policyLifecycleSnapshot';
import {
  policyTransitionRejectionMessage,
} from '@/components/policy-compliance/policyTransitionPreviewModel';
import { QualityPanel } from '@/components/quality';
import {
  AccessibleTabList,
  AccessibleTabPanel,
} from '@/components/shared/AccessibleTabs';
import {
  PreviousResultsSection,
  TechnicalAuditSection,
  ValidationCycleHeader,
  ValidationCycleStatusBadge,
  type ValidationCycleState,
} from '@/components/validation-cycle/ValidationCyclePrimitives';
import { useDashboardApi } from '@/services/api';
import type {
  SpecStatus,
  SpecValidationCycleSummary,
  ValidationCycleCheckSummary,
  ValidationTechnicalAudit,
} from '@/types';

import { SpecChecklistPanel } from './SpecChecklistPanel';
import { SpecValidationHistoryPanel } from './SpecValidationHistoryPanel';
import { resolveSpecSemanticAnchor } from './specSemanticAnchors';

type ValidationSubTab =
  | 'spec-validation'
  | 'checklist'
  | 'requirement-lint'
  | 'policy-compliance';

interface SpecValidationPanelProps {
  boardId: string;
  specId: string;
  specVersion: number;
  specEdition: number;
  specStatus: SpecStatus;
  canReadChecklist: boolean;
  canExecuteChecklist: boolean;
  canReadValidation: boolean;
  canReadQuality: boolean;
  canReadPolicyCompliance: boolean;
  /** Requirement text by stable child id, quoted inside lint findings. */
  anchorTexts?: Record<string, string>;
  policyTransitionPreview: PolicyTransitionPreviewLoadState;
  policyTransitionRejection?: PolicyTransitionRejection | null;
  specArchived: boolean;
  validationHistoryRefreshKey?: number;
  onAssessmentRecorded?: () => void;
  onPolicyEvaluated?: () => void;
  onOpenRequirementLintHelp?: () => void;
  onSubmitValidation?: () => void;
  canSubmitValidation?: boolean;
}

interface CurrentValidationCardProps {
  edition: number;
  state: ValidationCycleState;
  headline: string;
  description: string;
  children: ReactNode;
}

function CurrentValidationCard({
  edition,
  state,
  headline,
  description,
  children,
}: CurrentValidationCardProps) {
  return (
    <section
      className={`overflow-hidden rounded-xl border ${
        state === 'passed'
          ? 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-800 dark:bg-emerald-950/20'
          : state === 'failed'
            ? 'border-red-200 bg-red-50/60 dark:border-red-800 dark:bg-red-950/20'
            : state === 'in_progress'
              ? 'border-violet-200 bg-violet-50/60 dark:border-violet-800 dark:bg-violet-950/20'
              : 'border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/30'
      }`}
      data-testid="spec-validation-current"
    >
      <header className="flex w-full items-start justify-between gap-3 p-4 text-left">
        <span>
          <span className="block text-[11px] font-semibold uppercase tracking-wide text-surface-500 dark:text-surface-400">
            Current validation
          </span>
          <span className="mt-1 block text-sm font-semibold text-surface-900 dark:text-white">
            {headline}
          </span>
          <span className="mt-2 block text-xs text-surface-500 dark:text-surface-400">
            {description}
          </span>
        </span>
        <span className="flex shrink-0 items-center">
          <ValidationCycleStatusBadge state={state} />
        </span>
      </header>
      <div
        className="border-t border-surface-200 p-4 dark:border-surface-700"
        data-edition={edition}
      >
        {children}
      </div>
    </section>
  );
}

function normalizedValidationState(status: string | undefined): ValidationCycleState | null {
  switch (status?.trim().toLowerCase()) {
    case 'not_started':
    case 'missing':
      return 'not_started';
    case 'pending':
    case 'running':
    case 'in_progress':
      return 'in_progress';
    case 'success':
    case 'pass':
    case 'passed':
    case 'approved':
      return 'passed';
    case 'failed':
    case 'fail':
    case 'rejected':
      return 'failed';
    case 'blocked':
    case 'warning':
    case 'needs_attention':
      return 'needs_attention';
    case 'completed':
    case 'recorded':
    case 'current':
      return 'completed';
    default:
      return null;
  }
}

function specValidationState(
  status: SpecStatus,
  currentResultStatus: string | null,
  cycleState: SpecValidationCycleSummary['cycle_state'] | null,
  loading: boolean,
): ValidationCycleState {
  if (currentResultStatus) {
    return normalizedValidationState(currentResultStatus) ?? 'completed';
  }
  if (loading) return status === 'draft' ? 'not_started' : 'in_progress';
  if (cycleState === 'in_progress' || cycleState === 'pending') return 'in_progress';
  if (status === 'draft' || status === 'review') return 'not_started';
  if (status === 'approved') return 'in_progress';
  // Without an explicit current result, post-validation lifecycle states are
  // inconclusive. Only a persisted failed outcome is presented as Failed.
  return 'needs_attention';
}

function checkValidationState(
  check: ValidationCycleCheckSummary | undefined,
  loading: boolean,
): ValidationCycleState {
  if (loading) return 'in_progress';
  if (!check) return 'not_started';
  if (check.status.trim().toLowerCase() === 'off') return 'completed';
  const state = normalizedValidationState(check.status);
  if (state !== 'completed') return state ?? 'needs_attention';
  return normalizedValidationState(check.summary) ?? 'completed';
}

interface PolicyCheckPresentation {
  state: ValidationCycleState;
  summary: string;
  badgeLabel?: string;
}

function quantity(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function humanJoin(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? '';
  if (parts.length === 2) return `${parts[0]} and ${parts[1]}`;
  return `${parts.slice(0, -1).join(', ')}, and ${parts.at(-1)}`;
}

function policyContextSuffix(contextOnly: number): string {
  if (contextOnly === 0) return '';
  return ` ${quantity(contextOnly, 'adopted guideline')} ${contextOnly === 1 ? 'has' : 'have'} no Spec metrics and ${contextOnly === 1 ? 'is' : 'are'} excluded from the applicable total.`;
}

function policyResolvedBreakdown(
  counts: NonNullable<ReturnType<typeof parsePolicyComplianceLifecycleDetails>>['counts'],
): string {
  const parts = [
    counts.passed > 0 ? `${counts.passed} passed` : null,
    counts.waived > 0 ? `${counts.waived} waived` : null,
    counts.skipped > 0 ? `${counts.skipped} skipped` : null,
  ].filter((part): part is string => part !== null);
  return parts.length > 0 ? ` (${parts.join(', ')})` : '';
}

function policyWaiverMetricSummary(
  counts: NonNullable<ReturnType<typeof parsePolicyComplianceLifecycleDetails>>['counts'],
): string {
  if (counts.waived_metrics === 0) return '';
  const covered = `${quantity(counts.waived_metrics, 'failed metric finding')} ${counts.waived_metrics === 1 ? 'is covered by an approved waiver' : 'are covered by approved waivers'}`;
  const residual = counts.unwaived_failed_metrics > 0
    ? `; ${quantity(counts.unwaived_failed_metrics, 'failed metric finding')} ${counts.unwaived_failed_metrics === 1 ? 'remains' : 'remain'} unresolved`
    : '';
  return ` ${covered}${residual}.`;
}

function advisoryIssueSummary(
  counts: NonNullable<ReturnType<typeof parsePolicyComplianceLifecycleDetails>>['counts'],
): string {
  return [
    counts.advisory_failed > 0
      ? `${quantity(counts.advisory_failed, 'advisory policy', 'advisory policies')} ${counts.advisory_failed === 1 ? 'has' : 'have'} findings`
      : null,
    counts.advisory_pending > 0
      ? `${quantity(counts.advisory_pending, 'advisory policy', 'advisory policies')} ${counts.advisory_pending === 1 ? 'awaits' : 'await'} assessment`
      : null,
  ].filter((part): part is string => part !== null).join('; ');
}

function policyCheckPresentation(
  check: ValidationCycleCheckSummary | undefined,
  loading: boolean,
): PolicyCheckPresentation {
  if (loading) {
    return {
      state: 'in_progress',
      summary: 'Loading applicable policies for this edition…',
    };
  }
  const snapshot = parsePolicyComplianceLifecycleDetails(check);
  if (!snapshot) {
    return {
      state: 'needs_attention',
      badgeLabel: 'Unavailable',
      summary: 'The frozen policy scope for this edition could not be verified.',
    };
  }
  const { counts } = snapshot;
  const contextSuffix = policyContextSuffix(counts.context_only);
  const resolvedBreakdown = policyResolvedBreakdown(counts);
  const advisoryIssues = advisoryIssueSummary(counts);
  const waiverSummary = policyWaiverMetricSummary(counts);
  if (counts.inconsistent > 0 || counts.scope_inconsistent > 0) {
    const unavailable = [
      counts.inconsistent > 0
        ? quantity(counts.inconsistent, 'applicable policy')
        : null,
      counts.scope_inconsistent > 0
        ? quantity(counts.scope_inconsistent, 'frozen scope item')
        : null,
    ].filter((item): item is string => item !== null).join(' and ');
    return {
      state: 'needs_attention',
      badgeLabel: 'Unavailable',
      summary: `${unavailable} could not be reconciled safely for this edition. Verified applicable policies remain visible below.${contextSuffix}`,
    };
  }
  if (counts.applicable === 0) {
    return {
      state: 'completed',
      badgeLabel: 'Not required',
      summary: `No policies apply to this Spec edition.${contextSuffix}`,
    };
  }
  if (counts.blocking_failed > 0) {
    return {
      state: 'needs_attention',
      summary: `${counts.completed} of ${counts.applicable} applicable policies assessed${resolvedBreakdown}; ${quantity(counts.blocking_failed, 'blocking policy', 'blocking policies')} failed.${waiverSummary}${advisoryIssues ? ` ${advisoryIssues}; these advisory items do not block validation.` : ''}${contextSuffix}`,
    };
  }
  if (counts.blocking_pending > 0) {
    return {
      state: counts.completed === 0 ? 'not_started' : 'in_progress',
      summary: `${counts.completed} of ${counts.applicable} applicable policies assessed${resolvedBreakdown}; ${quantity(counts.blocking_pending, 'blocking policy', 'blocking policies')} ${counts.blocking_pending === 1 ? 'awaits' : 'await'} assessment.${waiverSummary}${advisoryIssues ? ` ${advisoryIssues}; these advisory items do not block validation.` : ''}${contextSuffix}`,
    };
  }
  if (
    check?.status.trim().toLowerCase() === 'advisory'
    || counts.advisory_failed > 0
    || counts.advisory_pending > 0
  ) {
    return {
      state: 'completed',
      badgeLabel: 'Advisory',
      summary: `${counts.completed} of ${counts.applicable} applicable policies assessed${resolvedBreakdown}; ${advisoryIssues || 'advisory review remains open'}. These advisory items do not block validation.${waiverSummary}${contextSuffix}`,
    };
  }
  if (counts.skipped > 0 || counts.waived > 0) {
    const resolutionParts = [
      counts.passed > 0 ? `${counts.passed} passed` : null,
      counts.waived > 0 ? `${counts.waived} waived` : null,
      counts.skipped > 0 ? `${counts.skipped} skipped` : null,
    ].filter((part): part is string => part !== null);
    const badgeLabel = counts.waived > 0 && counts.skipped > 0
      ? 'Resolved with exceptions'
      : counts.waived > 0
        ? counts.passed === 0 ? 'Waived' : 'Passed with waivers'
        : counts.passed === 0 ? 'Skipped' : 'Passed with skips';
    return {
      state: 'passed',
      badgeLabel,
      summary: `All ${counts.applicable} applicable ${counts.applicable === 1 ? 'policy is' : 'policies are'} resolved: ${humanJoin(resolutionParts)}.${waiverSummary}${contextSuffix}`,
    };
  }
  return {
    state: 'passed',
    summary: `All ${counts.applicable} applicable ${counts.applicable === 1 ? 'policy passed' : 'policies passed'}.${contextSuffix}`,
  };
}

function humanizeAction(value: string): string {
  const normalized = value.split('_').join(' ').trim();
  return normalized
    ? `${normalized.charAt(0).toUpperCase()}${normalized.slice(1)}`
    : value;
}

export function SpecValidationPanel({
  boardId,
  specId,
  specVersion,
  specEdition,
  specStatus,
  canReadChecklist,
  canExecuteChecklist,
  canReadValidation,
  canReadQuality,
  canReadPolicyCompliance,
  anchorTexts,
  policyTransitionPreview,
  policyTransitionRejection = null,
  specArchived,
  validationHistoryRefreshKey = 0,
  onAssessmentRecorded,
  onPolicyEvaluated,
  onOpenRequirementLintHelp,
  onSubmitValidation,
  canSubmitValidation = false,
}: SpecValidationPanelProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const tabIdPrefix = useId();
  const tabs = useMemo(() => [
    ...(canReadValidation
      ? [{ id: 'spec-validation' as const, label: 'Spec Validation' }]
      : []),
    ...(canReadChecklist
      ? [{ id: 'checklist' as const, label: 'Checklist' }]
      : []),
    ...(canReadQuality
      ? [{ id: 'requirement-lint' as const, label: 'Requirement lint' }]
      : []),
    ...(canReadPolicyCompliance
      ? [{ id: 'policy-compliance' as const, label: 'Policy Compliance' }]
      : []),
  ], [
    canReadChecklist,
    canReadPolicyCompliance,
    canReadQuality,
    canReadValidation,
  ]);
  const [activeTab, setActiveTab] = useState<ValidationSubTab>(
    () => tabs[0]?.id ?? 'spec-validation',
  );
  const [previousExpanded, setPreviousExpanded] = useState(false);
  const [technicalAuditExpanded, setTechnicalAuditExpanded] = useState(false);
  const [cycleSummary, setCycleSummary] =
    useState<SpecValidationCycleSummary | null>(null);
  const [cycleLoading, setCycleLoading] = useState(true);
  const [cycleError, setCycleError] = useState<string | null>(null);
  const [cycleReloadKey, setCycleReloadKey] = useState(0);
  const [technicalAudit, setTechnicalAudit] =
    useState<ValidationTechnicalAudit | null>(null);
  const [technicalAuditLoading, setTechnicalAuditLoading] = useState(false);
  const [technicalAuditError, setTechnicalAuditError] = useState<string | null>(null);
  const technicalAuditLoadKeyRef = useRef<string | null>(null);

  const resolveSemanticAnchor = useMemo(
    () => (
      anchor: Parameters<typeof resolveSpecSemanticAnchor>[0],
    ) => resolveSpecSemanticAnchor(anchor, anchorTexts),
    [anchorTexts],
  );

  useEffect(() => {
    if (!(
      canReadChecklist
      || canReadValidation
      || canReadQuality
      || canReadPolicyCompliance
    )) {
      setCycleSummary(null);
      setCycleLoading(false);
      setCycleError(null);
      return undefined;
    }
    const controller = new AbortController();
    setCycleLoading(true);
    setCycleError(null);
    apiRef.current.getValidationCycle('spec', specId, {
      includePrevious: false,
      signal: controller.signal,
    })
      .then((summary) => {
        if (controller.signal.aborted) return;
        if (
          summary.subject_type !== 'spec'
          || summary.subject_id !== specId
          || summary.edition !== specEdition
        ) {
          throw new Error('The validation-cycle summary does not match this Spec edition.');
        }
        setCycleSummary(summary);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setCycleSummary(null);
          setCycleError(
            error instanceof Error
              ? error.message
              : 'Validation-cycle summary could not be loaded.',
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setCycleLoading(false);
      });
    return () => controller.abort();
  }, [
    canReadChecklist,
    canReadPolicyCompliance,
    canReadQuality,
    canReadValidation,
    cycleReloadKey,
    specEdition,
    specId,
    validationHistoryRefreshKey,
  ]);

  const checksByType = useMemo(() => new Map(
    (cycleSummary?.checks ?? []).map((check) => [check.result_type, check]),
  ), [cycleSummary]);
  const lintCheck = checksByType.get('requirement_lint');
  const checklistCheck = checksByType.get('curated_checklist');
  const policyCheck = checksByType.get('policy_compliance');
  const policySnapshot = useMemo(
    () => parsePolicyComplianceLifecycleDetails(policyCheck),
    [policyCheck],
  );
  const lintState = checkValidationState(lintCheck, cycleLoading);
  const checklistState = checkValidationState(checklistCheck, cycleLoading);
  const checklistNotRequired = checklistCheck?.status.trim().toLowerCase() === 'off';
  const policyPresentation = policyCheckPresentation(policyCheck, cycleLoading);
  const policyState = policyPresentation.state;

  useEffect(() => {
    if (!tabs.some((tab) => tab.id === activeTab) && tabs[0]) {
      setActiveTab(tabs[0].id);
    }
  }, [activeTab, tabs]);

  const currentResult = cycleSummary?.current_result?.subject_edition === specEdition
    && cycleSummary.current_result.result_type === 'spec_validation'
    ? cycleSummary.current_result
    : null;
  const validationState = specValidationState(
    specStatus,
    currentResult?.status ?? null,
    cycleSummary?.cycle_state ?? null,
    cycleLoading,
  );
  const refreshCycle = () => setCycleReloadKey((value) => value + 1);
  const currentHeadline = validationState === 'not_started'
    ? `No result for Edition ${specEdition}`
    : cycleLoading
      ? 'Loading current validation…'
      : validationState === 'in_progress'
        ? `Edition ${specEdition} is ready for validation`
        : validationState === 'passed'
          ? `Edition ${specEdition} validation is complete`
          : validationState === 'completed'
            ? `Edition ${specEdition} validation is recorded`
            : `Edition ${specEdition} needs attention`;
  const currentDescription = validationState === 'not_started'
    ? 'The validation workspace creates a new current result when this Spec enters its validation stage.'
    : currentResult
      ? 'The current validation result for this edition is shown below.'
      : 'Review the checks below for the current edition.';

  useEffect(() => {
    if (!canReadValidation || !technicalAuditExpanded || !currentResult) {
      return undefined;
    }
    const loadKey = [
      specId,
      specEdition,
      currentResult.result_type,
      currentResult.result_id,
    ].join(':');
    if (technicalAuditLoadKeyRef.current === loadKey) return undefined;
    const controller = new AbortController();
    setTechnicalAuditLoading(true);
    setTechnicalAuditError(null);
    apiRef.current.getValidationTechnicalAudit(
      'spec',
      specId,
      currentResult.result_id,
      'spec_validation',
      controller.signal,
    ).then((audit) => {
      if (controller.signal.aborted) return;
      if (
        audit.result_id !== currentResult.result_id
        || audit.subject_type !== 'spec'
        || audit.subject_id !== specId
        || audit.result_type !== 'spec_validation'
        || audit.subject_edition !== specEdition
      ) {
        throw new Error('The technical audit does not match this Spec edition.');
      }
      technicalAuditLoadKeyRef.current = loadKey;
      setTechnicalAudit(audit);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        setTechnicalAudit(null);
        setTechnicalAuditError(
          error instanceof Error ? error.message : 'Technical audit could not be loaded.',
        );
      }
    }).finally(() => {
      if (!controller.signal.aborted) setTechnicalAuditLoading(false);
    });
    return () => controller.abort();
  }, [
    canReadValidation,
    currentResult,
    specEdition,
    specId,
    technicalAuditExpanded,
  ]);

  return (
    <div className="space-y-4 p-4" data-testid="spec-validation-workspace">
      <ValidationCycleHeader
        title="Validation"
        edition={specEdition}
        description="Each section keeps one current result for this lifecycle edition and preserves earlier results as history."
        icon={<FileCheck2 size={18} className="text-violet-600 dark:text-violet-300" aria-hidden="true" />}
        actions={(
          <span className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={refreshCycle}
              disabled={cycleLoading}
              aria-label="Refresh validation cycle"
              className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-surface-300 bg-white px-3 py-2 text-xs font-semibold text-surface-700 hover:bg-surface-50 disabled:opacity-50 dark:border-surface-600 dark:bg-surface-800 dark:text-surface-200"
            >
              <RefreshCw size={14} className={cycleLoading ? 'animate-spin' : ''} aria-hidden="true" />
              Refresh
            </button>
            {canSubmitValidation && onSubmitValidation && (
              <button
                type="button"
                onClick={onSubmitValidation}
                className="inline-flex min-h-9 items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-2 text-xs font-semibold text-white shadow-sm hover:bg-violet-700"
              >
                <CheckCircle2 size={14} aria-hidden="true" />
                Submit validation
              </button>
            )}
          </span>
        )}
      />

      {cycleError && (
        <p
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
        >
          Validation-cycle state could not be verified. {cycleError}
        </p>
      )}

      <AccessibleTabList
        idBase={tabIdPrefix}
        ariaLabel="Spec validation sections"
        items={tabs}
        value={activeTab}
        onValueChange={setActiveTab}
        variant="secondary"
      />

      {canReadValidation && (
        <AccessibleTabPanel
          idBase={tabIdPrefix}
          tabId="spec-validation"
          value={activeTab}
          mount="lazy-keep"
          className="space-y-4"
        >
          <CurrentValidationCard
            edition={specEdition}
            state={validationState}
            headline={currentHeadline}
            description={currentDescription}
          >
            <SpecValidationHistoryPanel
              specId={specId}
              currentEdition={specEdition}
              refreshKey={validationHistoryRefreshKey}
              view="current"
              anchorTexts={anchorTexts}
            />
          </CurrentValidationCard>

          {(cycleSummary?.remaining_actions.length ?? 0) > 0 && (
            <section
              className="rounded-xl border border-amber-200 bg-amber-50/70 p-4 dark:border-amber-800 dark:bg-amber-950/25"
              aria-labelledby="spec-validation-remaining-actions-title"
            >
              <h4 id="spec-validation-remaining-actions-title" className="text-xs font-semibold text-amber-900 dark:text-amber-100">
                Remaining actions
              </h4>
              <ul className="mt-2 list-inside list-disc space-y-1 text-xs text-amber-800 dark:text-amber-200">
                {cycleSummary!.remaining_actions.map((action) => (
                  <li key={action}>{humanizeAction(action)}</li>
                ))}
              </ul>
            </section>
          )}

          <PreviousResultsSection
            expanded={previousExpanded}
            onToggle={() => setPreviousExpanded((value) => !value)}
            count={cycleSummary?.previous_result_count}
            title="Previous validations"
            description="Earlier validation attempts and completed editions."
            testId="spec-validation-previous"
          >
            <SpecValidationHistoryPanel
              specId={specId}
              currentEdition={specEdition}
              refreshKey={validationHistoryRefreshKey}
              view="previous"
              anchorTexts={anchorTexts}
            />
          </PreviousResultsSection>

          <TechnicalAuditSection
            expanded={technicalAuditExpanded}
            onToggle={() => setTechnicalAuditExpanded((value) => !value)}
            testId="spec-validation-technical-audit"
          >
            {technicalAuditLoading ? (
              <p role="status" className="text-xs text-surface-500 dark:text-surface-400">
                Loading technical audit…
              </p>
            ) : technicalAuditError ? (
              <p role="alert" className="text-xs text-red-700 dark:text-red-300">
                Technical audit could not be loaded. {technicalAuditError}
              </p>
            ) : technicalAudit && currentResult
              && technicalAudit.result_id === currentResult.result_id ? (
              <dl className="grid gap-2 text-xs sm:grid-cols-2">
                <div>
                  <dt className="text-surface-500 dark:text-surface-400">Result identifier</dt>
                  <dd className="mt-0.5 break-all font-mono text-surface-800 dark:text-surface-100">{technicalAudit.result_id}</dd>
                </div>
                <div>
                  <dt className="text-surface-500 dark:text-surface-400">Processing fence</dt>
                  <dd className="mt-0.5 font-mono text-surface-800 dark:text-surface-100">
                    subject r{technicalAudit.technical_audit.subject_version} · head r{technicalAudit.technical_audit.head_revision}
                  </dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-surface-500 dark:text-surface-400">Immutable record</dt>
                  <dd className="mt-0.5 break-all font-mono text-surface-800 dark:text-surface-100">{technicalAudit.technical_audit.receipt_id}</dd>
                </div>
              </dl>
            ) : (
              <p className="text-xs text-surface-500 dark:text-surface-400">
                No technical record exists for the current edition.
              </p>
            )}
          </TechnicalAuditSection>
        </AccessibleTabPanel>
      )}

      {canReadChecklist && (
        <AccessibleTabPanel idBase={tabIdPrefix} tabId="checklist" value={activeTab} mount="lazy-keep" className="space-y-3">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-surface-200 bg-surface-50 px-3 py-2 dark:border-surface-700 dark:bg-surface-900/40" data-testid="spec-validation-checklist-summary">
            <p className="text-xs text-surface-600 dark:text-surface-300">
              {checklistNotRequired ? 'Checklist is disabled for this board.' : checklistCheck?.summary || 'Required checklist result for the current edition.'}
            </p>
            <ValidationCycleStatusBadge state={checklistState} label={checklistNotRequired ? 'Not required' : undefined} />
          </div>
          <SpecChecklistPanel
            boardId={boardId}
            specId={specId}
            expectedSpecVersion={specVersion}
            expectedSpecEdition={specEdition}
            canRead={canReadChecklist}
            canExecute={canExecuteChecklist}
            validationStageActive={specStatus === 'approved'}
            showHistory
            presentationMode="lifecycle-edition"
            embedded
            onStateChange={refreshCycle}
          />
        </AccessibleTabPanel>
      )}

      {canReadQuality && (
        <AccessibleTabPanel idBase={tabIdPrefix} tabId="requirement-lint" value={activeTab} mount="lazy-keep" className="space-y-3">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-surface-200 bg-surface-50 px-3 py-2 dark:border-surface-700 dark:bg-surface-900/40" data-testid="spec-validation-lint-summary">
            <p className="text-xs text-surface-600 dark:text-surface-300">{lintCheck?.summary || 'Advisory observations for requirements in this edition.'}</p>
            <ValidationCycleStatusBadge state={lintState} />
          </div>
          <QualityPanel
            subjectType="spec"
            subjectId={specId}
            subjectVersion={specVersion}
            subjectEdition={specEdition}
            subjectStatus={specStatus}
            subjectArchived={specArchived}
            canRead={canReadQuality}
            canAssess={false}
            canProposeQuestions={false}
            anchorTexts={anchorTexts}
            presentationMode="lifecycle-edition"
            embedded
            onAssessmentRecorded={() => {
              refreshCycle();
              onAssessmentRecorded?.();
            }}
            onOpenHelp={onOpenRequirementLintHelp}
          />
        </AccessibleTabPanel>
      )}

      {canReadPolicyCompliance && (
        <AccessibleTabPanel idBase={tabIdPrefix} tabId="policy-compliance" value={activeTab} mount="lazy-keep" className="space-y-3">
          <div className="flex items-center justify-between gap-3 rounded-lg border border-surface-200 bg-surface-50 px-3 py-2 dark:border-surface-700 dark:bg-surface-900/40" data-testid="spec-validation-policy-summary">
            <p className="text-xs text-surface-600 dark:text-surface-300">{policyPresentation.summary}</p>
            <ValidationCycleStatusBadge
              state={policyState}
              label={policyPresentation.badgeLabel}
            />
          </div>
          {policyTransitionRejection && (
            <p
              role="status"
              data-testid="spec-validation-policy-transition-attempt"
              className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200"
            >
              Last transition attempt: {policyTransitionRejectionMessage(
                policyTransitionRejection,
                'lifecycle-edition',
              )}. The frozen edition summary above is unchanged.
            </p>
          )}
          <PolicyCompliancePanel
            boardId={boardId}
            entityType="spec"
            subjectId={specId}
            subjectVersion={specVersion}
            subjectEdition={specEdition}
            presentationMode="lifecycle-edition"
            embedded
            transitionPreview={policyTransitionPreview}
            lifecycleSnapshot={cycleLoading ? undefined : policySnapshot}
            refreshKey={validationHistoryRefreshKey}
            resolveSemanticAnchor={resolveSemanticAnchor}
            onEvaluated={() => {
              refreshCycle();
              onPolicyEvaluated?.();
            }}
            onRefreshed={() => {
              refreshCycle();
              onPolicyEvaluated?.();
            }}
          />
        </AccessibleTabPanel>
      )}
    </div>
  );
}
