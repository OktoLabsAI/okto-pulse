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
  ChevronDown,
  ChevronUp,
  FileCheck2,
  ListChecks,
  RefreshCw,
  ShieldCheck,
  SpellCheck2,
} from 'lucide-react';

import {
  PolicyCompliancePanel,
  PolicyComplianceTransitionPreview,
  type PolicyTransitionRejection,
  type PolicyTransitionPreviewLoadState,
} from '@/components/policy-compliance';
import { QualityPanel } from '@/components/quality';
import {
  PreviousResultsSection,
  TechnicalAuditSection,
  ValidationCycleHeader,
  ValidationCycleStatusBadge,
  type ValidationCycleState,
} from '@/components/validation-cycle/ValidationCyclePrimitives';
import { useDashboardApi } from '@/services/api';
import { measureValidationWorkspaceInteraction } from '@/services/validation-workspace-telemetry';
import type {
  SpecStatus,
  SpecValidationCycleSummary,
  ValidationCycleCheckSummary,
  ValidationTechnicalAudit,
} from '@/types';

import { SpecChecklistPanel } from './SpecChecklistPanel';
import { SpecValidationHistoryPanel } from './SpecValidationHistoryPanel';
import { resolveSpecSemanticAnchor } from './specSemanticAnchors';

type ValidationDetail =
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

interface ValidationCheckRowProps {
  icon: ReactNode;
  title: string;
  description: string;
  state: ValidationCycleState;
  badgeLabel?: string;
  expanded: boolean;
  mounted: boolean;
  onToggle: () => void;
  children: ReactNode;
  testId: string;
}

function ValidationCheckRow({
  icon,
  title,
  description,
  state,
  badgeLabel,
  expanded,
  mounted,
  onToggle,
  children,
  testId,
}: ValidationCheckRowProps) {
  const contentId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const handleToggle = () => {
    measureValidationWorkspaceInteraction('validation_check', expanded, onToggle);
  };
  return (
    <article
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || !expanded) return;
        event.preventDefault();
        event.stopPropagation();
        handleToggle();
        triggerRef.current?.focus();
      }}
      className="overflow-hidden rounded-xl border border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-900/30"
      data-testid={testId}
    >
      <button
        ref={triggerRef}
        type="button"
        onClick={handleToggle}
        aria-expanded={expanded}
        aria-controls={contentId}
        className="flex w-full items-center justify-between gap-4 p-4 text-left transition-colors hover:bg-surface-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-500 dark:hover:bg-surface-800/60"
      >
        <span className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 rounded-lg bg-surface-100 p-2 text-surface-600 dark:bg-surface-800 dark:text-surface-300">
            {icon}
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-semibold text-surface-900 dark:text-white">
              {title}
            </span>
            <span className="mt-0.5 block text-xs text-surface-500 dark:text-surface-400">
              {description}
            </span>
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <ValidationCycleStatusBadge state={state} label={badgeLabel} />
          {expanded
            ? <ChevronUp size={16} className="text-surface-500" aria-hidden="true" />
            : <ChevronDown size={16} className="text-surface-500" aria-hidden="true" />}
        </span>
      </button>
      {mounted && (
        <div
          id={contentId}
          hidden={!expanded}
          className="border-t border-surface-200 p-4 dark:border-surface-700"
        >
          {children}
        </div>
      )}
    </article>
  );
}

interface CurrentValidationCardProps {
  edition: number;
  state: ValidationCycleState;
  headline: string;
  description: string;
  expanded: boolean;
  mounted: boolean;
  onToggle: () => void;
  children: ReactNode;
}

function CurrentValidationCard({
  edition,
  state,
  headline,
  description,
  expanded,
  mounted,
  onToggle,
  children,
}: CurrentValidationCardProps) {
  const contentId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const handleToggle = () => {
    measureValidationWorkspaceInteraction('current_validation', expanded, onToggle);
  };
  return (
    <section
      onKeyDown={(event) => {
        if (event.key !== 'Escape' || !expanded) return;
        event.preventDefault();
        event.stopPropagation();
        handleToggle();
        triggerRef.current?.focus();
      }}
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
      <button
        ref={triggerRef}
        type="button"
        onClick={handleToggle}
        aria-expanded={expanded}
        aria-controls={contentId}
        className="flex w-full items-start justify-between gap-3 p-4 text-left hover:bg-white/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-500 dark:hover:bg-surface-900/20"
      >
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
        <span className="flex shrink-0 items-center gap-2">
          <ValidationCycleStatusBadge state={state} />
          {expanded
            ? <ChevronUp size={16} className="text-surface-500" aria-hidden="true" />
            : <ChevronDown size={16} className="text-surface-500" aria-hidden="true" />}
        </span>
      </button>
      {mounted && (
        <div
          id={contentId}
          hidden={!expanded}
          className="border-t border-surface-200 p-4 dark:border-surface-700"
          data-edition={edition}
        >
          {children}
        </div>
      )}
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
  const [activeDetail, setActiveDetail] = useState<ValidationDetail | null>(null);
  const [openedDetails, setOpenedDetails] = useState<Set<ValidationDetail>>(
    () => new Set(),
  );
  const [currentExpanded, setCurrentExpanded] = useState(false);
  const [currentOpened, setCurrentOpened] = useState(false);
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
  const lintState = checkValidationState(lintCheck, cycleLoading);
  const checklistState = checkValidationState(checklistCheck, cycleLoading);
  const checklistNotRequired = checklistCheck?.status.trim().toLowerCase() === 'off';
  const policyState = policyTransitionRejection
    ? 'needs_attention'
    : checkValidationState(policyCheck, cycleLoading);

  const toggleDetail = (detail: ValidationDetail) => {
    setOpenedDetails((current) => {
      if (current.has(detail)) return current;
      const next = new Set(current);
      next.add(detail);
      return next;
    });
    setCurrentExpanded(false);
    setActiveDetail((current) => current === detail ? null : detail);
  };

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
  const toggleCurrent = () => {
    setCurrentOpened(true);
    setActiveDetail(null);
    setCurrentExpanded((value) => !value);
  };
  const currentHeadline = validationState === 'not_started'
    ? `No result for Edition ${specEdition}`
    : cycleLoading
      ? 'Loading current validation…'
      : validationState === 'in_progress'
        ? `Edition ${specEdition} is ready for validation`
        : validationState === 'passed'
          ? `Edition ${specEdition} validation is complete`
          : `Edition ${specEdition} needs attention`;
  const currentDescription = validationState === 'not_started'
    ? 'The validation workspace creates a new current result when this Spec enters its validation stage.'
    : currentResult
      ? 'A current validation result is available for this edition. Open to review its score and summary.'
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
        title="Spec Validation"
        edition={specEdition}
        description={canReadValidation
          ? 'One current validation result is kept for each edition. Earlier attempts remain available below.'
          : 'Current checks are grouped here for this edition.'}
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

      {canReadValidation && (
        <CurrentValidationCard
          edition={specEdition}
          state={validationState}
          headline={currentHeadline}
          description={currentDescription}
          expanded={currentExpanded}
          mounted={currentOpened}
          onToggle={toggleCurrent}
        >
          <SpecValidationHistoryPanel
            specId={specId}
            currentEdition={specEdition}
            refreshKey={validationHistoryRefreshKey}
            view="current"
          />
        </CurrentValidationCard>
      )}

      <div className="space-y-2" aria-label="Current validation checks">
        {canReadChecklist && (
          <ValidationCheckRow
            icon={<ListChecks size={16} aria-hidden="true" />}
            title="Curated checklist"
            description={checklistNotRequired
              ? 'Checklist is disabled for this board.'
              : checklistCheck?.summary
                || 'Required checklist result for the current edition.'}
            state={checklistState}
            badgeLabel={checklistNotRequired ? 'Not required' : undefined}
            expanded={activeDetail === 'checklist'}
            mounted={openedDetails.has('checklist')}
            onToggle={() => toggleDetail('checklist')}
            testId="spec-validation-checklist-row"
          >
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
              onStateChange={() => refreshCycle()}
            />
          </ValidationCheckRow>
        )}

        {canReadQuality && (
          <ValidationCheckRow
            icon={<SpellCheck2 size={16} aria-hidden="true" />}
            title="Requirement lint"
            description={lintCheck?.summary
              || 'Advisory observations for requirements in this edition.'}
            state={lintState}
            expanded={activeDetail === 'requirement-lint'}
            mounted={openedDetails.has('requirement-lint')}
            onToggle={() => toggleDetail('requirement-lint')}
            testId="spec-validation-lint-row"
          >
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
          </ValidationCheckRow>
        )}

        {canReadPolicyCompliance && (
          <ValidationCheckRow
            icon={<ShieldCheck size={16} aria-hidden="true" />}
            title="Policy compliance"
            description={policyCheck?.summary
              || 'Applicable guideline results for the current edition.'}
            state={policyState}
            expanded={activeDetail === 'policy-compliance'}
            mounted={openedDetails.has('policy-compliance')}
            onToggle={() => toggleDetail('policy-compliance')}
            testId="spec-validation-policy-row"
          >
            <PolicyCompliancePanel
              boardId={boardId}
              entityType="spec"
              subjectId={specId}
              subjectVersion={specVersion}
              subjectEdition={specEdition}
              presentationMode="lifecycle-edition"
              embedded
              transitionPreview={policyTransitionPreview}
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
          </ValidationCheckRow>
        )}
      </div>

      {(cycleSummary?.remaining_actions.length ?? 0) > 0 && (
        <section
          className="rounded-xl border border-amber-200 bg-amber-50/70 p-4 dark:border-amber-800 dark:bg-amber-950/25"
          aria-labelledby="spec-validation-remaining-actions-title"
        >
          <h4
            id="spec-validation-remaining-actions-title"
            className="text-xs font-semibold text-amber-900 dark:text-amber-100"
          >
            Remaining actions
          </h4>
          <ul className="mt-2 list-inside list-disc space-y-1 text-xs text-amber-800 dark:text-amber-200">
            {cycleSummary!.remaining_actions.map((action) => (
              <li key={action}>{humanizeAction(action)}</li>
            ))}
          </ul>
        </section>
      )}

      {canReadValidation && (
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
          />
        </PreviousResultsSection>
      )}

      {canReadValidation && (
        <TechnicalAuditSection
          expanded={technicalAuditExpanded}
          onToggle={() => setTechnicalAuditExpanded((value) => !value)}
          testId="spec-validation-technical-audit"
        >
          <div className="space-y-3">
            {technicalAuditLoading ? (
              <p role="status" className="text-xs text-surface-500 dark:text-surface-400">
                Loading technical audit…
              </p>
            ) : technicalAuditError ? (
              <p role="alert" className="text-xs text-red-700 dark:text-red-300">
                Technical audit could not be loaded. {technicalAuditError}
              </p>
            ) : technicalAudit
              && currentResult
              && technicalAudit.result_id === currentResult.result_id ? (
              <dl className="grid gap-2 text-xs sm:grid-cols-2">
                <div>
                  <dt className="text-surface-500 dark:text-surface-400">Result identifier</dt>
                  <dd className="mt-0.5 break-all font-mono text-surface-800 dark:text-surface-100">
                    {technicalAudit.result_id}
                  </dd>
                </div>
                <div>
                  <dt className="text-surface-500 dark:text-surface-400">Processing fence</dt>
                  <dd className="mt-0.5 font-mono text-surface-800 dark:text-surface-100">
                    subject r{technicalAudit.technical_audit.subject_version} · head r{technicalAudit.technical_audit.head_revision}
                  </dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-surface-500 dark:text-surface-400">Immutable record</dt>
                  <dd className="mt-0.5 break-all font-mono text-surface-800 dark:text-surface-100">
                    {technicalAudit.technical_audit.receipt_id}
                  </dd>
                </div>
              </dl>
            ) : (
              <p className="text-xs text-surface-500 dark:text-surface-400">
                No technical record exists for the current edition.
              </p>
            )}
            {canReadPolicyCompliance && (
              <PolicyComplianceTransitionPreview
                preview={policyTransitionPreview}
                rejection={policyTransitionRejection}
              />
            )}
          </div>
        </TechnicalAuditSection>
      )}
    </div>
  );
}
