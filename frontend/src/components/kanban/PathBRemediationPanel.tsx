// Path B amendment-lineage remediation panel (spec be089cd3 / card b002b7ca).
//
// Sibling of BugWorkflowRemediationPanel: that panel summarizes the gate +
// candidate scenarios; THIS panel shows Path A / Path B / Path C as DISTINCT
// concepts, the amendment lineage + coverage states, reason codes, and the SAFE
// remediation actions (create amendment / associate revision / open resolver
// details). It NEVER offers a skip/bypass/override — the gate is only remediated.
import { useState } from 'react';
import { AlertCircle, Check, ChevronDown, ChevronRight, Link2, Plus } from 'lucide-react';

import type {
  AmendmentPathBResolution,
  AmendmentRevision,
  BugRegressionScenarioPreview,
} from '@/types';

type Tone = 'green' | 'amber' | 'red' | 'neutral';

const TONE_CLASS: Record<Tone, string> = {
  green: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  amber: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  red: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  neutral: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
};

// Tolerant to BOTH coverage vocabularies. pending/coverage_pending is NEVER
// closure-ready; validated/path_b_ready is shown validated but still does not
// promise closure if another gate blocks.
function coverageView(state?: string | null): { label: string; tone: Tone; closureReady: boolean } {
  switch ((state || '').toLowerCase()) {
    case 'path_b_ready':
    case 'validated':
      return { label: 'Coverage validated', tone: 'green', closureReady: true };
    case 'coverage_pending':
    case 'pending':
      return { label: 'Coverage pending', tone: 'amber', closureReady: false };
    case 'missing':
      return { label: 'Coverage missing', tone: 'red', closureReady: false };
    default:
      return { label: 'Not applicable', tone: 'neutral', closureReady: false };
  }
}

function Badge({ tone, children, testid }: { tone: Tone; children: React.ReactNode; testid?: string }) {
  return (
    <span
      data-testid={testid}
      className={`inline-flex items-center rounded px-2 py-0.5 text-[10px] font-semibold ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

function PathConcept({
  label,
  active,
  description,
}: {
  label: string;
  active: boolean;
  description: string;
}) {
  return (
    <div
      className={`rounded border px-2 py-1.5 text-[11px] ${
        active
          ? 'border-gray-900 bg-gray-900 text-white dark:border-gray-100 dark:bg-gray-100 dark:text-gray-900'
          : 'border-gray-300 text-gray-600 dark:border-gray-600 dark:text-gray-300'
      }`}
    >
      <div className="font-semibold">{label}</div>
      <div className={`mt-0.5 ${active ? 'opacity-90' : 'opacity-70'}`}>{description}</div>
    </div>
  );
}

export function PathBRemediationPanel({
  revisions,
  pathBResolution,
  bugRegressionPreview,
  onCreateAmendment,
  onAssociate,
  busy = false,
}: {
  revisions: AmendmentRevision[];
  pathBResolution: AmendmentPathBResolution | null;
  bugRegressionPreview: BugRegressionScenarioPreview | null;
  onCreateAmendment: () => void;
  onAssociate: (amendmentId: string) => void;
  busy?: boolean;
}) {
  const [showResolver, setShowResolver] = useState(false);

  const remediation = bugRegressionPreview?.remediation ?? null;
  // Path B is required when the gate reports a semantic gap / Path B path, or an
  // amendment already exists. Otherwise Path A (same-spec) covers the bug.
  const pathBRequired =
    revisions.length > 0 ||
    (remediation?.remediation_path === 'path_b_semantic_gap') ||
    (bugRegressionPreview?.semantic_gap_required ?? false);
  const activePath: 'A' | 'B' | 'C' = pathBRequired ? 'B' : 'A';

  const coverage = coverageView(pathBResolution?.coverage_state);
  const missingLinks = pathBResolution?.missing_links ?? [];
  const safeActions = pathBResolution?.safe_next_actions ?? [];
  const rejected = pathBResolution?.rejected_scenarios ?? [];

  return (
    <div
      data-testid="path-b-remediation-panel"
      className="rounded-lg border border-gray-200 dark:border-gray-700 p-4 space-y-3"
    >
      {/* Path A / B / C as DISTINCT concepts (FR3) */}
      <div className="grid grid-cols-3 gap-2">
        <PathConcept label="Path A" active={activePath === 'A'} description="Same-spec regression scenario" />
        <PathConcept label="Path B" active={activePath === 'B'} description="Amendment lineage (cross-spec)" />
        <PathConcept label="Path C" active={false} description="Hotfix execution lane" />
      </div>
      <p className="text-[10px] text-gray-500 dark:text-gray-400" data-testid="path-c-not-substitute-note">
        Path C (hotfix lane) is only an execution lane. It does NOT replace Path B amendment lineage.
      </p>

      {!pathBRequired ? (
        <div
          data-testid="path-b-not-required"
          className="flex items-start gap-2 rounded border border-gray-200 dark:border-gray-700 p-3 text-sm"
        >
          <Check className="shrink-0 mt-0.5 text-green-500" size={16} />
          <span>
            <strong>Path B not required.</strong> Path A (same-spec regression) covers this bug — no amendment
            lineage is needed.
          </span>
        </div>
      ) : (
        <>
          {/* Coverage state (FR4) — pending never looks closure-ready */}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-semibold text-gray-600 dark:text-gray-300">Coverage</span>
            <Badge tone={coverage.tone} testid="coverage-state-badge">
              {coverage.label}
            </Badge>
            {!coverage.closureReady && (
              <span className="text-[10px] text-gray-500 dark:text-gray-400" data-testid="coverage-not-closure-ready">
                not closure-ready
              </span>
            )}
            {coverage.closureReady && (
              <span className="text-[10px] text-gray-500 dark:text-gray-400">
                (closure still subject to other gates)
              </span>
            )}
          </div>

          {/* Lineage state (FR4): missing links from the resolver */}
          <div className="rounded border border-gray-200 dark:border-gray-700 p-3">
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Required lineage
            </h4>
            {missingLinks.length > 0 ? (
              <ul className="mt-2 space-y-1 text-xs" data-testid="missing-links">
                {missingLinks.map((link) => (
                  <li key={link} className="flex items-center gap-2">
                    <AlertCircle className="shrink-0 text-red-500" size={12} />
                    <span>{link}</span>
                    <Badge tone="red">missing</Badge>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-xs text-green-700 dark:text-green-400" data-testid="lineage-eligible">
                Lineage eligible — all required links present.
              </p>
            )}
          </div>

          {/* Amendment revisions list */}
          <div className="rounded border border-gray-200 dark:border-gray-700 p-3">
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Amendment revisions
            </h4>
            {revisions.length > 0 ? (
              <ul className="mt-2 space-y-2" data-testid="amendment-revisions">
                {revisions.map((rev) => (
                  <li
                    key={rev.id}
                    data-testid={`amendment-revision-${rev.id}`}
                    className="flex flex-wrap items-center justify-between gap-2 rounded bg-gray-50 dark:bg-gray-800/50 px-2 py-1.5 text-xs"
                  >
                    <span className="font-mono">{rev.id}</span>
                    <span className="flex items-center gap-1.5">
                      <Badge tone="neutral">{rev.status}</Badge>
                      <Badge tone={rev.lineage_state === 'complete' ? 'green' : 'amber'}>
                        lineage: {rev.lineage_state}
                      </Badge>
                      {rev.eligibility?.reason_code && rev.eligibility.reason_code !== 'ok' && (
                        <Badge tone={rev.eligibility.blocked ? 'red' : 'amber'}>
                          {rev.eligibility.reason_code}
                        </Badge>
                      )}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onAssociate(rev.id)}
                        data-testid={`associate-${rev.id}`}
                        className="inline-flex items-center gap-1 rounded border border-gray-300 dark:border-gray-600 px-2 py-0.5 text-[10px] font-medium disabled:opacity-50"
                      >
                        <Link2 size={11} /> Associate revision
                      </button>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid="no-amendment-revisions">
                No amendment revision yet. Create one to start Path B lineage.
              </p>
            )}
          </div>

          {/* Safe actions (FR5: NO skip/bypass — create/associate only, user click) */}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onCreateAmendment}
              data-testid="create-amendment-action"
              className="inline-flex items-center gap-1.5 rounded bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-xs font-medium text-white dark:text-gray-900 disabled:opacity-50"
            >
              <Plus size={13} /> Create amendment revision
            </button>
            <button
              type="button"
              onClick={() => setShowResolver((v) => !v)}
              data-testid="toggle-resolver-details"
              className="inline-flex items-center gap-1 rounded border border-gray-300 dark:border-gray-600 px-3 py-1.5 text-xs font-medium"
            >
              {showResolver ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Resolver details
            </button>
          </div>

          {showResolver && (
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3 text-xs" data-testid="resolver-details">
              <div className="flex flex-wrap gap-2">
                {safeActions.map((action) => (
                  <Badge key={action} tone="neutral">{action}</Badge>
                ))}
              </div>
              {rejected.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {rejected.map((r) => (
                    <li key={r.scenario_id} className="flex items-center gap-2">
                      <span className="font-mono">{r.scenario_id}</span>
                      <Badge tone="red">{r.reason}</Badge>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
