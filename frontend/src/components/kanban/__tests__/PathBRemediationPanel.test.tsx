// Spec be089cd3 / card b002b7ca — PathBRemediationPanel component tests.
// Proves Path A/B/C are distinct, coverage states never look closure-ready when
// pending, no skip/bypass control exists, and create/associate fire only on
// user click (never on render).
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { PathBRemediationPanel } from '../PathBRemediationPanel';
import type {
  AmendmentRevision,
  AmendmentPathBResolution,
  BugRegressionScenarioPreview,
} from '@/types';

function revision(over: Partial<AmendmentRevision> = {}): AmendmentRevision {
  return {
    id: 'amd-1',
    board_id: 'b',
    original_spec_id: 'spec-1',
    origin_bug_id: 'bug-1',
    revision_spec_id: null,
    status: 'draft',
    lineage_state: 'incomplete',
    origin_task_ids: [],
    affected_task_ids: [],
    regression_scenario_ids: [],
    regression_test_task_ids: [],
    automated_regression_refs: [],
    eligibility: {
      lineage_eligible: false,
      canonicalization_candidate: false,
      blocked: true,
      reason_code: 'amendment_status_blocking',
    },
    ...over,
  };
}

const PATH_B_PREVIEW = {
  remediation: { remediation_path: 'path_b_semantic_gap' },
  semantic_gap_required: true,
  eligible_scenarios: [],
  rejected_scenarios: [],
} as unknown as BugRegressionScenarioPreview;

const PATH_A_PREVIEW = {
  remediation: { remediation_path: 'path_a_reuse_existing_scenario' },
  semantic_gap_required: false,
  eligible_scenarios: [{ scenario_id: 'ts_ok' }],
  rejected_scenarios: [],
} as unknown as BugRegressionScenarioPreview;

function renderPanel(props: {
  revisions?: AmendmentRevision[];
  pathBResolution?: AmendmentPathBResolution | null;
  preview?: BugRegressionScenarioPreview | null;
  onCreate?: () => void;
  onAssociate?: (id: string) => void;
}) {
  const onCreate = props.onCreate ?? vi.fn();
  const onAssociate = props.onAssociate ?? vi.fn();
  const utils = render(
    <PathBRemediationPanel
      revisions={props.revisions ?? []}
      pathBResolution={props.pathBResolution ?? null}
      bugRegressionPreview={props.preview ?? null}
      onCreateAmendment={onCreate}
      onAssociate={onAssociate}
    />,
  );
  return { ...utils, onCreate, onAssociate };
}

describe('PathBRemediationPanel', () => {
  it('shows Path A / Path B / Path C as distinct concepts and that Path C is not a substitute', () => {
    renderPanel({ preview: PATH_B_PREVIEW });
    expect(screen.getByText('Path A')).toBeInTheDocument();
    expect(screen.getByText('Path B')).toBeInTheDocument();
    expect(screen.getByText('Path C')).toBeInTheDocument();
    const note = screen.getByTestId('path-c-not-substitute-note');
    expect(note.textContent).toMatch(/does NOT replace Path B/i);
  });

  it('coverage_pending is shown NOT closure-ready', () => {
    renderPanel({
      preview: PATH_B_PREVIEW,
      revisions: [revision()],
      pathBResolution: { available: true, coverage_state: 'coverage_pending' },
    });
    expect(screen.getByTestId('coverage-state-badge').textContent).toMatch(/pending/i);
    expect(screen.getByTestId('coverage-not-closure-ready')).toBeInTheDocument();
  });

  it('tolerates the prose vocabulary: "pending" pending, "validated" validated', () => {
    const { rerender } = render(
      <PathBRemediationPanel
        revisions={[revision()]}
        pathBResolution={{ coverage_state: 'pending' }}
        bugRegressionPreview={PATH_B_PREVIEW}
        onCreateAmendment={vi.fn()}
        onAssociate={vi.fn()}
      />,
    );
    expect(screen.getByTestId('coverage-state-badge').textContent).toMatch(/pending/i);
    expect(screen.getByTestId('coverage-not-closure-ready')).toBeInTheDocument();

    rerender(
      <PathBRemediationPanel
        revisions={[revision()]}
        pathBResolution={{ coverage_state: 'validated' }}
        bugRegressionPreview={PATH_B_PREVIEW}
        onCreateAmendment={vi.fn()}
        onAssociate={vi.fn()}
      />,
    );
    expect(screen.getByTestId('coverage-state-badge').textContent).toMatch(/validated/i);
    expect(screen.queryByTestId('coverage-not-closure-ready')).toBeNull();
  });

  it('shows "Path B not required" when Path A covers the bug', () => {
    renderPanel({ preview: PATH_A_PREVIEW });
    expect(screen.getByTestId('path-b-not-required')).toBeInTheDocument();
    expect(screen.queryByTestId('create-amendment-action')).toBeNull();
  });

  it('renders missing lineage links + rejected reason codes via resolver details', () => {
    renderPanel({
      preview: PATH_B_PREVIEW,
      revisions: [revision()],
      pathBResolution: {
        available: true,
        coverage_state: 'coverage_pending',
        missing_links: ['regression_artifact'],
        safe_next_actions: ['confirm_validator_coverage'],
        rejected_scenarios: [{ scenario_id: 'ts_x', reason: 'unrelated_scenario' } as any],
      },
    });
    expect(screen.getByTestId('missing-links').textContent).toMatch(/regression_artifact/);
    // reason codes appear after expanding resolver details (no auto-expand).
    fireEvent.click(screen.getByTestId('toggle-resolver-details'));
    expect(screen.getByTestId('resolver-details').textContent).toMatch(/unrelated_scenario/);
  });

  it('exposes NO skip/bypass/override control anywhere (FR5)', () => {
    const { container } = renderPanel({
      preview: PATH_B_PREVIEW,
      revisions: [revision()],
      pathBResolution: { coverage_state: 'coverage_pending' },
    });
    expect(
      container.querySelector('[data-testid*="skip"],[data-testid*="bypass"],[data-testid*="override"],[data-testid*="force"]'),
    ).toBeNull();
    expect(container.textContent || '').not.toMatch(/skip the gate|bypass|override gate/i);
  });

  it('create/associate fire ONLY on user click — never on render', () => {
    const onCreate = vi.fn();
    const onAssociate = vi.fn();
    renderPanel({
      preview: PATH_B_PREVIEW,
      revisions: [revision({ id: 'amd-7' })],
      pathBResolution: { coverage_state: 'coverage_pending' },
      onCreate,
      onAssociate,
    });
    // not called on render (no auto-mutation).
    expect(onCreate).not.toHaveBeenCalled();
    expect(onAssociate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('create-amendment-action'));
    expect(onCreate).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('associate-amd-7'));
    expect(onAssociate).toHaveBeenCalledWith('amd-7');
  });

  // Card 62f6f196 / ts_b6d87391 — a blocked bug surfaces a create OR associate
  // amendment-lineage action (never a bypass).
  it('ts_b6d87391: blocked bug shows create action (no revision) and associate action (with revision)', () => {
    // No revision yet → only "Create amendment revision".
    const noRev = renderPanel({
      preview: PATH_B_PREVIEW,
      revisions: [],
      pathBResolution: {
        coverage_state: 'not_applicable',
        safe_next_actions: ['create_amendment_revision', 'escalate_semantic_gap'],
      } as AmendmentPathBResolution,
    });
    expect(screen.getByTestId('create-amendment-action')).toBeInTheDocument();
    expect(screen.getByTestId('no-amendment-revisions')).toBeInTheDocument();
    expect(screen.queryByTestId('associate-amd-9')).toBeNull();
    noRev.unmount();

    // A revision exists → associate action appears alongside create.
    renderPanel({
      preview: PATH_B_PREVIEW,
      revisions: [revision({ id: 'amd-9' })],
      pathBResolution: {
        coverage_state: 'coverage_pending',
        safe_next_actions: [
          'create_amendment_revision',
          'associate_amendment_revision_artifacts',
          'escalate_semantic_gap',
        ],
      } as AmendmentPathBResolution,
    });
    expect(screen.getByTestId('create-amendment-action')).toBeInTheDocument();
    expect(screen.getByTestId('associate-amd-9')).toBeInTheDocument();
  });

  // Card 62f6f196 / ts_5b0f1272 — lineage eligible but unconfirmed is coverage
  // pending and never closure-ready.
  it('ts_5b0f1272: lineage eligible + coverage_pending shows pending, never closure-ready', () => {
    renderPanel({
      preview: PATH_B_PREVIEW,
      revisions: [
        revision({
          id: 'amd-2',
          status: 'done',
          lineage_state: 'complete',
          eligibility: {
            lineage_eligible: true,
            canonicalization_candidate: false,
            blocked: false,
            reason_code: 'ok',
          },
        }),
      ],
      pathBResolution: {
        coverage_state: 'coverage_pending',
        missing_links: [],
        safe_next_actions: ['confirm_validator_coverage'],
      } as AmendmentPathBResolution,
    });
    // Lineage eligible (no missing links) but coverage still pending.
    expect(screen.getByTestId('lineage-eligible')).toBeInTheDocument();
    expect(screen.getByTestId('coverage-state-badge').textContent).toMatch(/pending/i);
    expect(screen.getByTestId('coverage-not-closure-ready')).toBeInTheDocument();
  });
});
