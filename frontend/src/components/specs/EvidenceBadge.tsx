/**
 * Inline badge that signals presence/absence of evidence for a test
 * scenario whose status is gated by NC-9 (automated/passed/failed). For
 * draft/ready scenarios the gate does not apply and the badge does not
 * render — callers can mount it unconditionally next to the status pill.
 *
 * Decision dec_470a95cc — binary semantic only (present vs absent), no
 * gradient of evidence quality. Tooltip surfaces the test_file_path /
 * last_run_at / test_function fields when present.
 */

import { Check, HelpCircle } from 'lucide-react';
import type { TestScenario, TestScenarioEvidence } from '@/types';

interface EvidenceBadgeProps {
  scenario: Pick<TestScenario, 'status' | 'evidence' | 'latest_evidence'>;
}

const GATED_STATUSES = new Set(['automated', 'passed', 'failed']);

function buildTooltip(evidence: TestScenario['evidence']): string {
  if (!evidence) {
    return 'No evidence linked. Marked without proof of real execution (or skip flag was ON).';
  }
  const parts: string[] = [];
  if (evidence.test_file_path) parts.push(`file: ${evidence.test_file_path}`);
  if (evidence.test_function) parts.push(`function: ${evidence.test_function}`);
  if (evidence.last_run_at) parts.push(`last run: ${evidence.last_run_at}`);
  if (evidence.test_run_id) parts.push(`run id: ${evidence.test_run_id}`);
  if (evidence.output_snippet) {
    const snippet = evidence.output_snippet.slice(0, 80);
    parts.push(`output: ${snippet}${evidence.output_snippet.length > 80 ? '…' : ''}`);
  }
  return parts.length > 0 ? parts.join(' | ') : 'evidence present';
}

function getScenarioEvidence(
  scenario: Pick<TestScenario, 'evidence' | 'latest_evidence'>,
): TestScenarioEvidence | null {
  return scenario.evidence ?? scenario.latest_evidence ?? null;
}

export function EvidenceBadge({ scenario }: EvidenceBadgeProps) {
  if (!GATED_STATUSES.has(scenario.status)) {
    return null;
  }

  const evidence = getScenarioEvidence(scenario);
  const hasEvidence = Boolean(
    evidence &&
      (evidence.test_file_path ||
        evidence.test_function ||
        evidence.last_run_at ||
        evidence.test_run_id ||
        evidence.output_snippet),
  );

  const tooltip = buildTooltip(evidence);

  if (hasEvidence) {
    return (
      <span
        title={tooltip}
        data-testid="evidence-badge-present"
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800/50"
      >
        <Check size={10} aria-hidden="true" />
        evidence
      </span>
    );
  }

  return (
    <span
      title={tooltip}
      data-testid="evidence-badge-missing"
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 border border-gray-300 dark:border-gray-700"
    >
      <HelpCircle size={10} aria-hidden="true" />
      no evidence
    </span>
  );
}
