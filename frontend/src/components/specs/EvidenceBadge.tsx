/**
 * Inline badge that signals evidence for a test scenario whose status is gated
 * by NC-9 (automated/passed/failed). For draft/ready scenarios the gate does
 * not apply and the badge does not render.
 *
 * Re-executable evidence contract (spec 9e0bf979, supersedes dec_470a95cc's
 * binary-only rule): when the evidence declares an `evidence_class`, the badge
 * reflects the REAL class and its replayability (replayable classes →
 * emerald; run_log / non_replayable_justified → amber) instead of a flat
 * present/absent signal. Legacy evidence without an `evidence_class` keeps the
 * original binary present/absent badge — the UI never INFERS a class, so it
 * cannot diverge from the backend contract. Tooltip surfaces the artifact,
 * expected output and justification so a validator never has to read comments.
 */

import { Check, HelpCircle, RotateCw, FileText } from 'lucide-react';
import type { TestScenario, TestScenarioEvidence, EvidenceClass } from '@/types';

interface EvidenceBadgeProps {
  scenario: Pick<TestScenario, 'status' | 'evidence' | 'latest_evidence'>;
}

const GATED_STATUSES = new Set(['automated', 'passed', 'failed']);

// Classes whose artifact lets a validator rerun/inspect deterministically.
const REPLAYABLE_CLASSES = new Set<EvidenceClass>([
  'automated_test_pointer',
  'replay_command',
  'mcp_replay_manifest',
  'manual_checklist',
]);

const CLASS_LABELS: Record<EvidenceClass, string> = {
  automated_test_pointer: 'auto test',
  replay_command: 'replay cmd',
  mcp_replay_manifest: 'MCP replay',
  manual_checklist: 'checklist',
  run_log: 'run log',
  non_replayable_justified: 'non-replayable',
};

function buildTooltip(evidence: TestScenarioEvidence | null): string {
  if (!evidence) {
    return 'No evidence linked. Marked without proof of real execution (or skip flag was ON).';
  }
  const parts: string[] = [];
  if (evidence.evidence_class) parts.push(`class: ${evidence.evidence_class}`);
  if (evidence.test_file_path) parts.push(`file: ${evidence.test_file_path}`);
  if (evidence.test_function) parts.push(`function: ${evidence.test_function}`);
  if (evidence.replay_command) parts.push(`replay: ${evidence.replay_command}`);
  if (evidence.mcp_replay_manifest) parts.push(`manifest: ${evidence.mcp_replay_manifest}`);
  if (evidence.manual_checklist_ref) parts.push(`checklist: ${evidence.manual_checklist_ref}`);
  if (evidence.last_run_at) parts.push(`last run: ${evidence.last_run_at}`);
  if (evidence.test_run_id) parts.push(`run id: ${evidence.test_run_id}`);
  if (evidence.expected_output_snapshot) {
    const snap = evidence.expected_output_snapshot.slice(0, 80);
    parts.push(`expected: ${snap}${evidence.expected_output_snapshot.length > 80 ? '…' : ''}`);
  }
  if (evidence.non_replayable_justification) {
    const just = evidence.non_replayable_justification.slice(0, 80);
    parts.push(`justification: ${just}${evidence.non_replayable_justification.length > 80 ? '…' : ''}`);
  }
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

function hasAnyEvidence(evidence: TestScenarioEvidence | null): boolean {
  return Boolean(
    evidence &&
      (evidence.test_file_path ||
        evidence.test_function ||
        evidence.last_run_at ||
        evidence.test_run_id ||
        evidence.output_snippet ||
        evidence.evidence_class ||
        evidence.replay_command ||
        evidence.mcp_replay_manifest ||
        evidence.manual_checklist_ref ||
        evidence.expected_output_snapshot ||
        evidence.non_replayable_justification),
  );
}

export function EvidenceBadge({ scenario }: EvidenceBadgeProps) {
  if (!GATED_STATUSES.has(scenario.status)) {
    return null;
  }

  const evidence = getScenarioEvidence(scenario);
  const hasEvidence = hasAnyEvidence(evidence);
  const tooltip = buildTooltip(evidence);
  const evidenceClass = evidence?.evidence_class ?? null;

  // Re-executable contract: reflect the real class + replayability when set.
  if (hasEvidence && evidenceClass) {
    const replayable = REPLAYABLE_CLASSES.has(evidenceClass);
    const label = CLASS_LABELS[evidenceClass] ?? evidenceClass;
    const Icon = replayable ? RotateCw : FileText;
    const className = replayable
      ? 'inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium bg-emerald-50 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800/50'
      : 'inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 border border-amber-200 dark:border-amber-800/50';
    return (
      <span
        title={tooltip}
        data-testid="evidence-badge-class"
        data-evidence-class={evidenceClass}
        data-replayable={replayable ? 'true' : 'false'}
        className={className}
      >
        <Icon size={10} aria-hidden="true" />
        {label}
      </span>
    );
  }

  // Legacy evidence (no evidence_class): original binary present/absent badge.
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
