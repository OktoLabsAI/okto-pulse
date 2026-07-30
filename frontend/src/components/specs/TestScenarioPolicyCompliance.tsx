import { useState } from 'react';
import { AlertTriangle, ArrowRight, LoaderCircle } from 'lucide-react';
import toast from 'react-hot-toast';

import {
  PolicyCompliancePanel,
  PolicyComplianceTransitionPreview,
  usePolicyTransitionAuthority,
} from '@/components/policy-compliance';
import { useDashboardApi } from '@/services/api';
import type {
  AllowedTransition,
  Spec,
  TestScenario,
  TestScenarioStatus,
} from '@/types';

const STATUS_COLORS: Record<TestScenarioStatus, string> = {
  draft:
    'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
  ready:
    'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
  automated:
    'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  passed:
    'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  failed:
    'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
};

function requiresAuthenticatedEvidence(
  transition: AllowedTransition,
): boolean {
  return transition.preconditions.includes('authenticated_test_evidence');
}

function isScenarioStatus(value: string): value is TestScenarioStatus {
  return (
    value === 'draft'
    || value === 'ready'
    || value === 'automated'
    || value === 'passed'
    || value === 'failed'
  );
}

export function TestScenarioStatusBadge({
  status,
}: {
  status: TestScenarioStatus;
}) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${STATUS_COLORS[status]}`}
      data-testid="test-scenario-status-badge"
    >
      {status}
    </span>
  );
}

export interface TestScenarioPolicyComplianceProps {
  boardId: string;
  specId: string;
  specArchived: boolean;
  scenario: TestScenario;
  canReadPolicyCompliance: boolean;
  refreshKey: number;
  onSpecRefreshed: (spec: Spec) => void;
}

/**
 * Scenario-scoped lifecycle and Policy Compliance surface.
 *
 * Scenario status is an operational child lifecycle. This component never
 * rewrites the parent spec's complete test_scenarios collection and never
 * manufactures authenticated evidence. Gated execution edges stay visible in
 * the authoritative preview but are not exposed as executable UI actions.
 */
export function TestScenarioPolicyCompliance({
  boardId,
  specId,
  specArchived,
  scenario,
  canReadPolicyCompliance,
  refreshKey,
  onSpecRefreshed,
}: TestScenarioPolicyComplianceProps) {
  const api = useDashboardApi();
  const [movingTo, setMovingTo] = useState<TestScenarioStatus | null>(null);
  const authority = usePolicyTransitionAuthority({
    boardId,
    entityType: 'test_scenario',
    subjectId: scenario.id,
    currentStatus: scenario.status,
    refreshKey,
  });

  const executableTransitions = authority.actionableTransitions.filter(
    (transition) =>
      isScenarioStatus(transition.to_status)
      && !requiresAuthenticatedEvidence(transition),
  );
  const evidenceTransitions = authority.actionableTransitions.filter(
    requiresAuthenticatedEvidence,
  );

  const refreshWholeSpec = async () => {
    const updated = await api.getSpec(specId);
    onSpecRefreshed(updated);
  };

  const moveScenario = async (transition: AllowedTransition) => {
    if (!isScenarioStatus(transition.to_status) || movingTo) return;
    const target = transition.to_status;
    authority.clearRejection();
    setMovingTo(target);
    try {
      await api.updateTestScenarioStatus(specId, scenario.id, {
        status: target,
      });
      await refreshWholeSpec();
      toast.success(`Test scenario moved to ${transition.label}`);
    } catch (caught) {
      const policyRejected = await authority.handleTransitionError(
        caught,
        target,
      );
      try {
        await refreshWholeSpec();
      } catch {
        // The transition error remains the primary diagnostic. The next modal
        // refresh will recover the parent projection.
      }
      toast.error(
        policyRejected
          ? 'Policy Compliance rejected this test scenario transition. Review the structured evidence below.'
          : caught instanceof Error
            ? caught.message
            : 'Failed to update test scenario status',
      );
    } finally {
      setMovingTo(null);
    }
  };

  return (
    <section
      className="space-y-3 border-t border-gray-200 pt-3 dark:border-gray-700"
      data-testid="test-scenario-policy-surface"
    >
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
          Lifecycle actions
        </h4>
        <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
          Actions come from the canonical server lifecycle. Execution outcomes
          that require authenticated evidence must be recorded through the
          evidence execution flow.
        </p>
      </div>

      {authority.preview.status === 'loading' && (
        <p
          className="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400"
          role="status"
        >
          <LoaderCircle size={13} className="animate-spin" aria-hidden="true" />
          Loading lifecycle actions…
        </p>
      )}

      {authority.preview.status === 'error' && (
        <p
          className="rounded-lg border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300"
          role="alert"
        >
          Lifecycle actions are unavailable. No transition is inferred.
        </p>
      )}

      {executableTransitions.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {executableTransitions.map((transition) => (
            <button
              key={transition.to_status}
              type="button"
              onClick={() => void moveScenario(transition)}
              disabled={movingTo !== null}
              className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-blue-300 bg-blue-50 px-3 py-1 text-xs font-semibold text-blue-700 hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-blue-700 dark:bg-blue-950/30 dark:text-blue-200"
            >
              {movingTo === transition.to_status ? (
                <LoaderCircle
                  size={13}
                  className="animate-spin"
                  aria-hidden="true"
                />
              ) : (
                <ArrowRight size={13} aria-hidden="true" />
              )}
              Move to {transition.label}
            </button>
          ))}
        </div>
      )}

      {authority.preview.status === 'ready'
        && executableTransitions.length === 0
        && evidenceTransitions.length === 0 && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          No lifecycle action is currently available.
        </p>
      )}

      {evidenceTransitions.length > 0 && (
        <p
          className="flex items-start gap-1.5 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-200"
          data-testid="test-scenario-evidence-required"
        >
          <AlertTriangle
            size={13}
            className="mt-0.5 shrink-0"
            aria-hidden="true"
          />
          <span>
            Authenticated evidence is required before moving to{' '}
            {evidenceTransitions
              .map((transition) => transition.label)
              .join(', ')}
            . These edges are preview-only here.
          </span>
        </p>
      )}

      {canReadPolicyCompliance && (
        <div
          className="space-y-3"
          data-testid="test-scenario-policy-compliance"
        >
          <PolicyComplianceTransitionPreview
            preview={authority.preview}
            rejection={authority.rejection}
          />
          <PolicyCompliancePanel
            boardId={boardId}
            entityType="test_scenario"
            subjectId={scenario.id}
            evaluationEnabled={!specArchived}
            evaluationUnavailableReason={
              specArchived
                ? 'Restore the parent spec before evaluating this test scenario.'
                : undefined
            }
            refreshKey={refreshKey}
            onEvaluated={() => {
              authority.clearRejection();
              void authority.refresh();
            }}
            onRefreshed={() => {
              authority.clearRejection();
              void authority.refresh();
            }}
          />
        </div>
      )}
    </section>
  );
}
