import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clipboard,
  ExternalLink,
  History,
  Info,
  LockKeyhole,
  RefreshCw,
} from 'lucide-react';

import type {
  SemanticPinpointTechnicalDetails,
  SemanticPinpointViewModel,
  SemanticPolicyUiState,
} from './semanticPolicyModel';

export interface ActionablePinpointProps {
  pinpoint: SemanticPinpointViewModel;
  policyState: SemanticPolicyUiState;
  onNavigate?: (target: string) => void;
  onCopy?: (text: string) => Promise<void> | void;
}

const SEVERITY_TONES = {
  low: 'bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300',
  medium:
    'bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-300',
  high: 'bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300',
  critical: 'bg-red-700 text-white dark:bg-red-500 dark:text-surface-950',
} as const;

const STATE_LABELS: Record<SemanticPolicyUiState, string> = {
  fail: 'Current issue',
  positive_evidence: 'Current evidence',
  non_blocking_warning: 'Non-blocking warning',
  waived_fail_finding: 'Waiver active',
  stale: 'Stale evidence',
  legacy: 'Legacy read-only',
  removed: 'Removed location',
  inaccessible: 'Restricted location',
  loading: 'Loading',
  no_assessment: 'No assessment',
  no_visible_pinpoints: 'No visible pinpoints',
  recoverable_transport_error: 'Refresh error',
};

function technicalDetailsText(
  details: SemanticPinpointTechnicalDetails,
): string {
  return [
    ['Anchor type', details.anchorType],
    ['Source version', details.sourceVersion],
    ['Anchor reference', details.anchorReference],
    ['Excerpt SHA-256', details.excerptHash],
    ['Input SHA-256', details.inputDigest],
    ['Metric result SHA-256', details.metricResultDigest],
  ]
    .filter((entry): entry is [string, string] => typeof entry[1] === 'string')
    .map(([label, value]) => `${label}: ${value}`)
    .join('\n');
}

async function copyText(value: string): Promise<void> {
  if (!navigator.clipboard?.writeText) {
    throw new Error('Clipboard unavailable');
  }
  await navigator.clipboard.writeText(value);
}

function StateIcon({ state }: { state: SemanticPolicyUiState }) {
  if (state === 'fail') {
    return <AlertTriangle size={13} aria-hidden="true" />;
  }
  if (
    state === 'stale'
    || state === 'non_blocking_warning'
    || state === 'waived_fail_finding'
  ) {
    return <Info size={13} aria-hidden="true" />;
  }
  if (state === 'inaccessible') {
    return <LockKeyhole size={13} aria-hidden="true" />;
  }
  return <CheckCircle2 size={13} aria-hidden="true" />;
}

/**
 * Semantic counterpart of QualityPanel FindingItems. It deliberately accepts
 * a dedicated view model so policy evidence never acquires QualityFinding
 * domain semantics.
 */
export function ActionablePinpoint({
  pinpoint,
  policyState,
  onNavigate,
  onCopy,
}: ActionablePinpointProps) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>(
    'idle',
  );
  const resetTimer = useRef<number | null>(null);
  const detailsText = useMemo(
    () => pinpoint.technicalDetails
      ? technicalDetailsText(pinpoint.technicalDetails)
      : '',
    [pinpoint.technicalDetails],
  );

  useEffect(() => () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
  }, []);

  const handleCopy = async () => {
    try {
      await (onCopy ?? copyText)(detailsText);
      setCopyStatus('copied');
    } catch {
      setCopyStatus('failed');
    }
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    resetTimer.current = window.setTimeout(() => setCopyStatus('idle'), 2000);
  };

  return (
    <article
      className="rounded-lg border border-surface-200 bg-white p-3 shadow-sm dark:border-surface-700 dark:bg-surface-900/50"
      data-testid="actionable-pinpoint"
      data-contract-version={pinpoint.contractVersion}
      data-state={pinpoint.state}
    >
      <div
        className="flex flex-wrap items-center gap-1.5"
        data-testid="actionable-pinpoint-badges"
      >
        {pinpoint.severity && (
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ${SEVERITY_TONES[pinpoint.severity]}`}
          >
            {pinpoint.severity}
          </span>
        )}
        <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold uppercase text-blue-700 dark:bg-blue-950/50 dark:text-blue-300">
          {pinpoint.kind === 'legacy' ? 'Legacy location' : pinpoint.kind}
        </span>
        <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold uppercase text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">
          {pinpoint.categoryLabel}
        </span>
        <span className="inline-flex items-center gap-1 rounded-full bg-surface-100 px-2 py-0.5 text-[10px] font-semibold uppercase text-surface-600 dark:bg-surface-800 dark:text-surface-300">
          <StateIcon state={policyState} />
          {STATE_LABELS[policyState]}
        </span>
      </div>

      <h4
        className="mt-3 text-sm font-semibold text-surface-900 dark:text-surface-100"
        data-testid="actionable-pinpoint-title"
      >
        {pinpoint.title}
      </h4>
      <p
        className="mt-2 whitespace-pre-wrap text-xs leading-5 text-surface-700 dark:text-surface-300"
        data-testid="actionable-pinpoint-detail"
      >
        {pinpoint.detail}
      </p>

      <section className="mt-3" aria-label="Assessment location">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-surface-500 dark:text-surface-400">
          Location
        </p>
        <p className="mt-1 text-xs font-semibold text-surface-800 dark:text-surface-100">
          {pinpoint.locationLabel}
        </p>
        {pinpoint.excerpt && (
          <blockquote
            className="mt-2 border-l-2 border-violet-300 bg-surface-50 py-1.5 pl-3 pr-2 text-xs italic text-surface-700 dark:border-violet-700 dark:bg-surface-950/50 dark:text-surface-200"
            data-testid="actionable-pinpoint-excerpt"
          >
            {pinpoint.excerpt}
          </blockquote>
        )}
        {pinpoint.unavailableMessage && (
          <p
            className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-surface-600 dark:text-surface-300"
            role="status"
          >
            {pinpoint.state === 'inaccessible'
              ? <LockKeyhole size={13} aria-hidden="true" />
              : <Info size={13} aria-hidden="true" />}
            {pinpoint.unavailableMessage}
          </p>
        )}
      </section>

      {pinpoint.remediation && (
        <p
          className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200"
          data-testid="actionable-pinpoint-remediation"
        >
          <span className="font-semibold">Suggested remediation:</span>{' '}
          {pinpoint.remediation}
        </p>
      )}

      {pinpoint.navigationTarget && onNavigate && (
        <button
          type="button"
          onClick={() => onNavigate(pinpoint.navigationTarget as string)}
          className="mt-3 inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-violet-300 bg-white px-3 py-1 text-xs font-semibold text-violet-700 hover:bg-violet-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-600 dark:border-violet-700 dark:bg-surface-900 dark:text-violet-200"
        >
          <ExternalLink size={13} aria-hidden="true" />
          Go to location
        </button>
      )}

      {pinpoint.technicalDetails && (
        <details className="mt-3 rounded-lg border border-surface-200 bg-surface-50 dark:border-surface-700 dark:bg-surface-950/40">
          <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-surface-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-600 dark:text-surface-200">
            Technical details
          </summary>
          <div className="space-y-2 border-t border-surface-200 px-3 py-2 dark:border-surface-700">
            <pre className="whitespace-pre-wrap break-all text-[10px] text-surface-600 dark:text-surface-300">
              {detailsText}
            </pre>
            <button
              type="button"
              onClick={() => void handleCopy()}
              className="inline-flex min-h-8 items-center gap-1.5 rounded border border-surface-300 bg-white px-2.5 py-1 text-xs font-semibold text-surface-700 hover:bg-surface-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-600 dark:border-surface-600 dark:bg-surface-900 dark:text-surface-200"
            >
              <Clipboard size={13} aria-hidden="true" />
              Copy technical details
            </button>
            <span className="sr-only" role="status" aria-live="polite">
              {copyStatus === 'copied'
                ? 'Technical details copied.'
                : copyStatus === 'failed'
                  ? 'Technical details could not be copied.'
                  : ''}
            </span>
          </div>
        </details>
      )}
    </article>
  );
}

export interface PolicyComplianceReadOnlyActionsProps {
  onViewHistory?: () => void;
  onRetry?: () => void;
  onViewReassessmentGuidance?: () => void;
  retrying?: boolean;
}

export function PolicyComplianceReadOnlyActions({
  onViewHistory,
  onRetry,
  onViewReassessmentGuidance,
  retrying = false,
}: PolicyComplianceReadOnlyActionsProps) {
  return (
    <nav
      aria-label="Policy compliance actions"
      className="flex flex-wrap items-center gap-2"
    >
      {onViewHistory && (
        <button type="button" onClick={onViewHistory} className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-surface-300 px-3 py-1 text-xs font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-600">
          <History size={13} aria-hidden="true" />
          Assessment history
        </button>
      )}
      {onRetry && (
        <button type="button" onClick={onRetry} disabled={retrying} className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-red-300 px-3 py-1 text-xs font-semibold text-red-700 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-600">
          <RefreshCw size={13} aria-hidden="true" className={retrying ? 'animate-spin' : ''} />
          {retrying ? 'Retrying…' : 'Retry'}
        </button>
      )}
      {onViewReassessmentGuidance && (
        <button type="button" onClick={onViewReassessmentGuidance} className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-amber-300 px-3 py-1 text-xs font-semibold text-amber-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-600">
          <Info size={13} aria-hidden="true" />
          View reassessment guidance
        </button>
      )}
    </nav>
  );
}
