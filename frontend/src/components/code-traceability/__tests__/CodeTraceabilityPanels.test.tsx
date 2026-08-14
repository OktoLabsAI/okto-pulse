import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import type {
  CodeInvestigationReceiptReadResult,
  CodeTraceabilityProjection,
} from '@/types';
import { CodeEvidencePanel } from '../CodeEvidencePanel';
import { EvidenceMatrixPanel } from '../EvidenceMatrixPanel';
import { ImplementationTargetsPanel } from '../ImplementationTargetsPanel';
import { ReceiptDetailModal } from '../ReceiptDetailModal';
import { subscribeContextualHelp } from '@/components/help/contextualHelp';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';

const apiMock = vi.hoisted(() => ({
  getCodeTraceabilityProjection: vi.fn(),
  getCodeInvestigationReceipt: vi.fn(),
  revokeCodeInvestigationReceipt: vi.fn(),
  revokeCodeEvidence: vi.fn(),
  createImplementationTarget: vi.fn(),
  acknowledgeImplementationOverlap: vi.fn(),
  createCodeTraceabilityWaiver: vi.fn(),
  clearCodeTraceabilityWaiver: vi.fn(),
}));

const permissionState = vi.hoisted(() => ({
  canRevoke: false,
  canRevokeEvidence: false,
  canCreateTarget: false,
  canAcknowledgeOverlap: false,
  canCreateWaiver: false,
  canClearWaiver: false,
}));

const clipboardWriteText = vi.fn();

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    has: (flag: string) => (
      (flag === 'code_traceability.investigation.revoke' && permissionState.canRevoke)
      || (flag === 'code_traceability.evidence.revoke' && permissionState.canRevokeEvidence)
      || (flag === 'code_traceability.target.create' && permissionState.canCreateTarget)
      || (flag === 'code_traceability.overlap.acknowledge' && permissionState.canAcknowledgeOverlap)
      || (flag === 'code_traceability.waiver.create' && permissionState.canCreateWaiver)
      || (flag === 'code_traceability.waiver.clear' && permissionState.canClearWaiver)
    ),
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
  }),
}));

const workspaceState = {
  workspace_state_id: 'sha256:workspace-1',
  declared_revision: 'revision-declared-by-agent',
  declared_dirty: true,
  observed_at: '2026-08-09T12:00:00Z',
  reproducibility_claim: 'worktree_snapshot',
  fingerprint_algorithm: 'agent-manifest-v1',
  manifest_digest: 'sha256:manifest',
  manifest_entry_count: 42,
};

const projection: CodeTraceabilityProjection = {
  subject_type: 'refinement',
  subject_id: 'ref-1',
  subject_version: 3,
  profile: 'detail',
  context_scope: 'default',
  evidence: [{
    id: 'evidence-1',
    investigation_receipt_id: 'receipt-1',
    source_ref: 'source:opaque-1',
    parent_type: 'refinement',
    parent_id: 'ref-1',
    parent_version: 3,
    evidence_type: 'code_observation',
    claim: 'The refresh persists before acquiring the lock.',
    workspace_state: workspaceState,
    selector_kind: 'qualified_symbol',
    relative_path: 'src/auth/token_service.py',
    language: 'python',
    symbol_kind: 'method',
    qualified_symbol: 'TokenService.refresh',
    snapshot_line_start: 120,
    snapshot_line_end: 158,
    attestation_state: 'agent_attested',
    lifecycle_status: 'active',
    supersedes_evidence_id: null,
  }],
  inherited_evidence_ids: ['evidence-1'],
  direct_evidence_ids: [],
  referenced_evidence_ids: [],
  links: [{
    id: 'link-1',
    evidence_id: 'evidence-1',
    spec_id: 'spec-1',
    entity_type: 'technical_requirement',
    entity_id: 'TR-2',
    relation_type: 'supports',
  }],
  dispositions: [],
  targets: [{
    id: 'target-1',
    card_id: 'card-1',
    source_ref: 'source:opaque-1',
    selector_kind: 'qualified_symbol',
    relative_path_hint: 'src/auth/token_service.py',
    qualified_symbol: 'TokenService.refresh',
    role: 'modify',
    intent: 'Acquire the lock before persistence.',
    required: true,
    lifecycle_status: 'active',
    revision: 1,
    current_resolution_id: 'resolution-1',
  }],
  resolutions: [{
    id: 'resolution-1',
    target_id: 'target-1',
    investigation_receipt_id: 'receipt-1',
    receipt_generation: 103,
    subject_version: 4,
    target_revision: 1,
    state: 'resolved',
    resolved_relative_path: 'src/auth/token_service.py',
    resolved_qualified_symbol: 'TokenService.refresh',
    resolved_line_start: 120,
    resolved_line_end: 158,
    confidence: 0.91,
  }],
  overlaps: [{
    target_a_id: 'target-1',
    target_b_id: 'target-2',
    resolution_a_id: 'resolution-1',
    resolution_b_id: 'resolution-2',
    severity: 'high',
    reason_code: 'same_symbol',
    relative_path: 'src/auth/token_service.py',
    qualified_symbol: 'TokenService.refresh',
    acknowledgement: null,
  }],
  waivers: [],
  heads: [{
    source_ref: 'source:opaque-1',
    generation: 103,
    current_receipt_id: 'receipt-1',
    state: 'current',
  }],
  counts: {},
  coverage: {
    total: 1,
    linked: 1,
    dispositioned: 0,
    pending: 0,
    pending_ids: [],
    coverage_pct: 100,
  },
  resolution_freshness: {
    'target-1': {
      state: 'resolved',
      currentness: 'current',
      resolution_id: 'resolution-1',
      target_revision: 1,
    },
  },
  gate_readiness: {
    mode: 'advisory',
    allowed: true,
    passed: true,
    blockers: [],
    receipt_currentness: { 'receipt-1': 'current' },
    resolution_freshness: {
      'target-1': {
        state: 'resolved',
        currentness: 'current',
        resolution_id: 'resolution-1',
        target_revision: 1,
      },
    },
  },
};

const receiptResult: CodeInvestigationReceiptReadResult = {
  currentness: 'current',
  receipt: {
    id: 'receipt-1',
    request_id: 'request-1',
    board_id: 'board-1',
    subject_type: 'refinement',
    subject_id: 'ref-1',
    subject_version: 3,
    attestor_actor_id: 'agent-1',
    generation: 104,
    predecessor_receipt_id: null,
    trust_level: 'single_attestation',
    acceptance_status: 'accepted',
    outcome: 'accessible',
    capabilities: ['file_read', 'symbol_search'],
    source_ref: 'source:opaque-1',
    source_identity_digest: 'sha256:source',
    canonicalization_profile: 'safe-v1',
    limits_profile: 'default-v1',
    selector_scope_digest: 'sha256:scope',
    declared_revision: 'revision-declared-by-agent',
    workspace_state: workspaceState,
    omission_manifest: [{
      reason_code: 'permission_denied',
      affected_scope_digest: 'a'.repeat(64),
      count: 2,
    }],
    omission_digest: 'sha256:omissions',
    omission_count: 1,
    tooling: { tool_id: 'external-agent', tool_version: '1', method_id: 'deterministic-read' },
    observed_at: '2026-08-09T12:00:00Z',
    received_at: '2026-08-09T12:00:03Z',
    expires_at: '2026-08-09T13:00:03Z',
    observation_sha256: 'sha256:observation',
    payload_sha256: 'sha256:payload',
  },
};

function NestedReceiptHarness() {
  const [parentOpen, setParentOpen] = useState(true);
  const [receiptOpen, setReceiptOpen] = useState(false);
  useEscapeToClose(() => setParentOpen(false), {
    enabled: parentOpen,
    priority: 0,
  });

  if (!parentOpen) return <div>Parent closed</div>;
  return (
    <div role="dialog" aria-label="Parent modal">
      <button type="button" onClick={() => setReceiptOpen(true)}>
        Open receipt detail
      </button>
      {receiptOpen && (
        <ReceiptDetailModal
          boardId="board-1"
          receiptId="receipt-1"
          onClose={() => setReceiptOpen(false)}
        />
      )}
    </div>
  );
}

beforeEach(() => {
  permissionState.canRevoke = false;
  permissionState.canRevokeEvidence = false;
  permissionState.canCreateTarget = false;
  permissionState.canAcknowledgeOverlap = false;
  permissionState.canCreateWaiver = false;
  permissionState.canClearWaiver = false;
  clipboardWriteText.mockReset();
  clipboardWriteText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: clipboardWriteText },
  });
  apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection);
  apiMock.getCodeInvestigationReceipt.mockResolvedValue(receiptResult);
  apiMock.revokeCodeInvestigationReceipt.mockResolvedValue({});
  apiMock.revokeCodeEvidence.mockResolvedValue({});
  apiMock.createImplementationTarget.mockResolvedValue({});
  apiMock.acknowledgeImplementationOverlap.mockResolvedValue({});
  apiMock.createCodeTraceabilityWaiver.mockResolvedValue({});
  apiMock.clearCodeTraceabilityWaiver.mockResolvedValue({});
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Code Traceability passive Community surfaces', () => {
  it('replaces repeated agent-mediated notices with contextual Help links', async () => {
    const helpListener = vi.fn();
    const unsubscribe = subscribeContextualHelp(helpListener);

    render(
      <>
        <CodeEvidencePanel boardId="board-1" subjectId="ref-1" subjectVersion={3} />
        <EvidenceMatrixPanel boardId="board-1" subjectId="spec-1" subjectVersion={7} />
      </>,
    );

    await waitFor(() => expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId('traceability-agent-mediated-disclosure')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Code evidence matrix' })).toBeInTheDocument();
    expect(screen.getByText(/Maps inherited Code Evidence/i)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('code-evidence-help-link'));
    expect(helpListener).toHaveBeenCalledWith({ sectionId: 'code-traceability' });

    fireEvent.click(screen.getByTestId('code-evidence-matrix-help-link'));
    expect(helpListener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it('renders accepted projections in the existing modal design without investigation controls', async () => {
    render(
      <>
        <CodeEvidencePanel boardId="board-1" subjectId="ref-1" subjectVersion={3} />
        <EvidenceMatrixPanel boardId="board-1" subjectId="spec-1" subjectVersion={7} />
        <ImplementationTargetsPanel boardId="board-1" subjectId="card-1" subjectVersion={4} specVersion={7} />
      </>,
    );

    await waitFor(() => expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(3));
    expect(await screen.findByTestId('refinement-code-evidence-panel')).toBeInTheDocument();
    expect(screen.getByTestId('spec-evidence-matrix-panel')).toBeInTheDocument();
    expect(screen.getByTestId('card-implementation-targets-panel')).toBeInTheDocument();
    expect(screen.getAllByText('Agent-attested').length).toBeGreaterThan(0);
    expect(screen.getByText('Receipt accepted')).toBeInTheDocument();
    expect(screen.getByText('Agent-attested resolution · PF-103')).toBeInTheDocument();
    expect(screen.getByText('Current against preflight PF-103')).toBeInTheDocument();
    expect(screen.getByText('Pulse cannot detect source changes until an agent submits a newer preflight receipt.')).toBeInTheDocument();

    for (const button of screen.getAllByRole('button')) {
      expect(button).not.toHaveAccessibleName(/connect|sync|clone|probe|resolve source|submit|start check/i);
    }
  });

  it('renders authoritative inherited-evidence coverage and includes IR/OR link columns', async () => {
    const matrixProjection: CodeTraceabilityProjection = {
      ...projection,
      evidence: [
        projection.evidence[0],
        {
          ...projection.evidence[0],
          id: 'evidence-2',
          investigation_receipt_id: 'receipt-2',
          claim: 'The operational alert remains pending disposition.',
        },
      ],
      inherited_evidence_ids: ['evidence-1', 'evidence-2'],
      links: [
        {
          ...projection.links[0],
          id: 'link-ir',
          entity_type: 'integration_requirement',
          entity_id: 'IR-7',
        },
        {
          ...projection.links[0],
          id: 'link-or',
          entity_type: 'observability_requirement',
          entity_id: 'OR-3',
        },
      ],
      coverage: {
        total: 2,
        linked: 1,
        dispositioned: 0,
        pending: 1,
        pending_ids: ['evidence-2'],
        coverage_pct: 50,
      },
      gate_readiness: {
        ...projection.gate_readiness,
        receipt_currentness: {
          'receipt-1': 'current',
          'receipt-2': 'expired',
        },
      },
    };
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(matrixProjection);

    render(
      <EvidenceMatrixPanel boardId="board-1" subjectId="spec-1" subjectVersion={7} />,
    );

    expect(await screen.findByText('1/2')).toBeInTheDocument();
    expect(screen.getByText('1', { selector: 'p' })).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByTestId('code-evidence-coverage-status')).toHaveTextContent('Pending');
    expect(screen.getByText('evidence items addressed')).toBeInTheDocument();
    expect(screen.getByText('evidence item pending')).toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'IR' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'OR' })).toBeInTheDocument();
    expect(screen.getByText('IR-7')).toBeInTheDocument();
    expect(screen.getByText('OR-3')).toBeInTheDocument();
    expect(screen.getByText('Current receipt')).toBeInTheDocument();
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledWith(
      'board-1',
      'spec',
      'spec-1',
      7,
      'detail',
      expect.any(AbortSignal),
      'gate',
    );
  });

  it('never presents a truncated gate projection as covered or skippable', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...projection,
      context_scope: 'gate',
      coverage: {
        total: 1,
        linked: 1,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        coverage_pct: 100,
        skipped: true,
      },
      gate_readiness: {
        ...projection.gate_readiness,
        blockers: [{
          code: 'code_traceability_projection_incomplete',
          message: 'Gate context exceeded a server-owned projection budget.',
          blocking: false,
        }],
      },
    });

    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        skipCoverage
        canEditCoverageFlags
        onSkipCoverageChange={vi.fn()}
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status')).toHaveTextContent('Incomplete');
    expect(screen.getByRole('alert')).toHaveTextContent('Validation remains blocked');
    expect(screen.getByRole('progressbar')).toHaveAttribute(
      'aria-valuetext',
      'Coverage projection incomplete',
    );
  });

  it('excludes direct or future evidence outside the authoritative inherited snapshot', async () => {
    const inheritedProjection: CodeTraceabilityProjection = {
      ...projection,
      evidence: [
        projection.evidence[0],
        {
          ...projection.evidence[0],
          id: 'evidence-future',
          claim: 'A finding recorded after the Spec snapshot.',
        },
        {
          ...projection.evidence[0],
          id: 'evidence-direct',
          claim: 'Direct evidence outside inherited matrix coverage.',
        },
      ],
      inherited_evidence_ids: ['evidence-1'],
      direct_evidence_ids: ['evidence-direct'],
      coverage: {
        total: 1,
        linked: 1,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        coverage_pct: 100,
      },
    };
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(inheritedProjection);

    render(
      <EvidenceMatrixPanel boardId="board-1" subjectId="spec-1" subjectVersion={7} />,
    );

    expect(await screen.findByText('1/1')).toBeInTheDocument();
    expect(screen.getByTestId('code-evidence-coverage-status')).toHaveTextContent('Covered');
    expect(screen.queryByText('A finding recorded after the Spec snapshot.')).not.toBeInTheDocument();
    expect(screen.queryByText('Direct evidence outside inherited matrix coverage.')).not.toBeInTheDocument();
  });

  it('shows the matrix coverage state and lets an authorized Draft editor skip it', async () => {
    const onSkipCoverageChange = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        skipCoverage={false}
        canEditCoverageFlags
        onSkipCoverageChange={onSkipCoverageChange}
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status')).toHaveTextContent('Covered');
    expect(screen.getByRole('progressbar', { name: 'Code Evidence coverage progress' }))
      .toHaveAttribute('aria-valuenow', '100');

    const toggle = screen.getByRole('switch', { name: 'Skip Code Evidence coverage' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(toggle);
    await waitFor(() => expect(onSkipCoverageChange).toHaveBeenCalledWith(true));

    rerender(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        skipCoverage
        canEditCoverageFlags
        onSkipCoverageChange={onSkipCoverageChange}
      />,
    );
    expect(screen.getByTestId('code-evidence-coverage-status')).toHaveTextContent('Skipped');
    expect(screen.getByRole('switch', { name: 'Skip Code Evidence coverage' }))
      .toHaveAttribute('aria-checked', 'true');
  });

  it('keeps the coverage state visible without exposing the skip control to read-only viewers', async () => {
    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        skipCoverage
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status')).toHaveTextContent('Skipped');
    expect(screen.queryByRole('switch', { name: 'Skip Code Evidence coverage' }))
      .not.toBeInTheDocument();
  });

  it('renders agent execution receipts with receipt currentness and resolution freshness', async () => {
    const executionProjection: CodeTraceabilityProjection = {
      ...projection,
      executions: [{
        id: 'execution-1',
        card_id: 'card-1',
        target_id: 'target-1',
        target_revision: 1,
        result_investigation_receipt_id: 'receipt-result-1',
        disposition: 'touched',
        source_ref: 'source:opaque-1',
        result_declared_revision: 'result-revision-104',
        actual_relative_path: 'src/auth/token_service.py',
        actual_qualified_symbol: 'TokenService.refresh',
        justification: 'Lock acquisition now precedes persistence.',
      }],
      resolution_freshness: {},
      gate_readiness: {
        ...projection.gate_readiness,
        receipt_currentness: {
          ...projection.gate_readiness.receipt_currentness,
          'receipt-result-1': 'current',
        },
        resolution_freshness: {
          'target-1': {
            state: 'resolved',
            currentness: 'expired',
            resolution_id: 'resolution-1',
            target_revision: 1,
          },
        },
      },
    };
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(executionProjection);

    render(
      <ImplementationTargetsPanel
        boardId="board-1"
        subjectId="card-1"
        subjectVersion={4}
        specVersion={7}
      />,
    );

    expect(await screen.findByText('Agent-submitted execution receipts')).toBeInTheDocument();
    expect(screen.getByText('Agent-submitted execution · touched')).toBeInTheDocument();
    expect(screen.getByText('TokenService.refresh · src/auth/token_service.py')).toBeInTheDocument();
    expect(screen.getByText('Lock acquisition now precedes persistence.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'View execution receipt' })).toBeInTheDocument();
    expect(screen.getByText('Current receipt')).toBeInTheDocument();
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(screen.getByText('expired · resolved')).toBeInTheDocument();
  });

  it('labels receipt acceptance without implying independent verification', async () => {
    render(
      <ReceiptDetailModal
        boardId="board-1"
        receiptId="receipt-1"
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText('Receipt accepted, not independently checked by Pulse')).toBeInTheDocument();
    expect(screen.getByText('Agent-declared capabilities')).toBeInTheDocument();
    expect(screen.getByText('Dirty (agent-declared)')).toBeInTheDocument();
    expect(screen.getByText('agent-manifest-v1')).toBeInTheDocument();
    expect(screen.getByText('permission denied')).toBeInTheDocument();
    expect(screen.getByText('· 2 affected')).toBeInTheDocument();
    expect(screen.getByText(`Scope digest: ${'a'.repeat(64)}`)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /revoke receipt/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
  });

  it('opens Submission guide on Refinement and Task and copies an agent-mediated workflow', async () => {
    render(
      <>
        <CodeEvidencePanel boardId="board-1" subjectId="ref-1" subjectVersion={3} />
        <ImplementationTargetsPanel
          boardId="board-1"
          subjectId="card-1"
          subjectVersion={4}
          specVersion={7}
        />
      </>,
    );

    const guides = await screen.findAllByRole('button', { name: 'Submission guide' });
    fireEvent.click(guides[0]);
    const refinementGuide = screen.getByRole('dialog', { name: 'Submission guide' });
    expect(within(refinementGuide).getByText(/No source check runs from this dialog/i)).toBeInTheDocument();
    expect(within(refinementGuide).getByText(/agent determines whether its own execution environment/i)).toBeInTheDocument();
    expect(within(refinementGuide).getByText(/okto_pulse_submit_code_evidence/i)).toBeInTheDocument();
    expect(within(refinementGuide).getByText(/Advisory does not mean disposable/i)).toBeInTheDocument();
    fireEvent.click(within(refinementGuide).getByRole('button', { name: 'Copy agent workflow' }));

    await waitFor(() => expect(clipboardWriteText).toHaveBeenCalledTimes(1));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(
      'okto_pulse_start_code_investigation',
    ));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(
      'Pulse Community only stores, projects and displays accepted records',
    ));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(
      'okto_pulse_submit_code_investigation_receipt',
    ));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(
      'okto-pulse://reference/code-traceability',
    ));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(
      'okto_pulse_link_code_evidence',
    ));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(
      'okto_pulse_set_code_evidence_disposition',
    ));
    expect(clipboardWriteText).toHaveBeenCalledWith(expect.stringContaining(
      'force the source survey, receipt and evidence work to be repeated',
    ));
    fireEvent.click(within(refinementGuide).getByRole('button', { name: 'Close submission guide' }));

    fireEvent.click(guides[1]);
    const taskGuide = screen.getByRole('dialog', { name: 'Submission guide' });
    expect(within(taskGuide).getByText('card:card-1@4')).toBeInTheDocument();
    expect(within(taskGuide).getByText(/okto_pulse_create_implementation_target/i)).toBeInTheDocument();
    fireEvent.click(within(taskGuide).getByRole('button', { name: 'Copy agent workflow' }));
    await waitFor(() => expect(clipboardWriteText).toHaveBeenCalledTimes(2));
    expect(clipboardWriteText).toHaveBeenLastCalledWith(expect.stringContaining(
      'okto_pulse_submit_implementation_target_resolution',
    ));
    expect(clipboardWriteText).toHaveBeenLastCalledWith(expect.stringContaining(
      'okto_pulse_submit_implementation_target_execution_receipt',
    ));
    expect(clipboardWriteText).toHaveBeenLastCalledWith(expect.stringContaining(
      'deleted, superseded or not_touched',
    ));
  });

  it('keeps operator revocation separate, permission-gated and append-only', async () => {
    permissionState.canRevoke = true;
    render(
      <ReceiptDetailModal
        boardId="board-1"
        receiptId="receipt-1"
        onClose={vi.fn()}
      />,
    );

    const revoke = await screen.findByRole('button', { name: 'Revoke receipt' });
    fireEvent.click(revoke);
    expect(await screen.findByText(/separate append-only operator record/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Append revocation' })).toBeDisabled();
  });

  it('records human Evidence revocation separately from the immutable attestation', async () => {
    permissionState.canRevokeEvidence = true;
    render(
      <CodeEvidencePanel boardId="board-1" subjectId="ref-1" subjectVersion={3} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke evidence' }));
    expect(screen.getByText(/Human governance action/i)).toBeInTheDocument();
    expect(screen.getByText(/never edits or replaces the immutable agent attestation/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Evidence revocation reason'), {
      target: { value: 'The evidence was linked to the wrong refinement revision.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm evidence revocation' }));

    await waitFor(() => expect(apiMock.revokeCodeEvidence).toHaveBeenCalledTimes(1));
    expect(apiMock.revokeCodeEvidence).toHaveBeenCalledWith(
      'board-1',
      'evidence-1',
      { reason: 'The evidence was linked to the wrong refinement revision.' },
    );
  });

  it.each([
    ['expired', 'Expired'],
    ['revoked', 'Revoked'],
    ['outdated', 'Historical'],
    [undefined, 'Currentness unknown'],
  ] as const)(
    'uses explicit gate currentness %s even when the head points at the receipt',
    async (currentness, expectedLabel) => {
      const currentnessProjection: CodeTraceabilityProjection = {
        ...projection,
        gate_readiness: {
          ...projection.gate_readiness,
          receipt_currentness: currentness ? { 'receipt-1': currentness } : {},
        },
      };
      apiMock.getCodeTraceabilityProjection.mockResolvedValue(currentnessProjection);

      render(
        <CodeEvidencePanel boardId="board-1" subjectId="ref-1" subjectVersion={3} />,
      );

      expect(await screen.findByText(expectedLabel)).toBeInTheDocument();
      expect(screen.queryByText('Current receipt')).not.toBeInTheDocument();
    },
  );

  it('keeps a historical resolution on its own receipt generation after the head advances', async () => {
    const historicalProjection: CodeTraceabilityProjection = {
      ...projection,
      heads: [{
        ...projection.heads[0],
        generation: 104,
        current_receipt_id: 'receipt-104',
      }],
      resolution_freshness: {
        'target-1': {
          state: 'resolved',
          currentness: 'outdated',
          resolution_id: 'resolution-1',
          target_revision: 1,
        },
      },
    };
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(historicalProjection);

    render(
      <ImplementationTargetsPanel
        boardId="board-1"
        subjectId="card-1"
        subjectVersion={4}
        specVersion={7}
      />,
    );

    expect(await screen.findByText('Agent-attested resolution · PF-103')).toBeInTheDocument();
    expect(screen.getByText('Historical')).toBeInTheDocument();
    expect(screen.queryByText(/PF-104/)).not.toBeInTheDocument();
  });

  it('creates a human semantic target with the canonical closed REST body', async () => {
    permissionState.canCreateTarget = true;
    render(
      <ImplementationTargetsPanel
        boardId="board-1"
        subjectId="card-1"
        subjectVersion={4}
        specVersion={7}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Add semantic target' }));
    const form = screen.getByRole('region', { name: 'Add semantic target' });
    fireEvent.change(within(form).getByLabelText('Target selector kind'), {
      target: { value: 'symbol' },
    });
    const submit = within(form).getByRole('button', { name: 'Add semantic target' });
    expect(submit).toBeDisabled();
    fireEvent.change(within(form).getByLabelText('Target qualified symbol'), {
      target: { value: 'PaymentsService.authorize' },
    });
    fireEvent.change(within(form).getByLabelText('Target intent'), {
      target: { value: 'Authorize the payment before persisting it.' },
    });
    fireEvent.click(submit);

    await waitFor(() => expect(apiMock.createImplementationTarget).toHaveBeenCalledTimes(1));
    expect(apiMock.createImplementationTarget).toHaveBeenCalledWith(
      'board-1',
      'card-1',
      {
        source_ref: 'source:opaque-1',
        selector_kind: 'symbol',
        relative_path_hint: null,
        language: null,
        symbol_kind: null,
        qualified_symbol: 'PaymentsService.authorize',
        symbol_signature: null,
        role: 'modify',
        intent: 'Authorize the payment before persisting it.',
        required: true,
        expected_spec_version: 7,
        baseline_evidence_id: null,
        spec_links: [],
        evidence_links: [],
      },
    );
  });

  it('keeps Rejected implementation targets readable while freezing human mutations', async () => {
    permissionState.canCreateTarget = true;
    permissionState.canCreateWaiver = true;
    permissionState.canClearWaiver = true;
    permissionState.canAcknowledgeOverlap = true;

    render(
      <ImplementationTargetsPanel
        boardId="board-1"
        subjectId="card-1"
        subjectVersion={5}
        specVersion={7}
        operationallyFrozen
        onCreateDependency={vi.fn()}
      />,
    );

    expect(await screen.findByText('Agent-attested resolution · PF-103'))
      .toBeInTheDocument();
    expect(screen.getByText(/This card is Rejected/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add semantic target' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create human waiver' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear waiver' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge overlap' }))
      .not.toBeInTheDocument();
  });

  it('records and clears a human waiver through the exact Card-scoped contracts', async () => {
    permissionState.canCreateWaiver = true;
    render(
      <ImplementationTargetsPanel
        boardId="board-1"
        subjectId="card-1"
        subjectVersion={4}
        specVersion={7}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Create human waiver' }));
    const form = screen.getByRole('region', { name: 'Create human waiver' });
    fireEvent.change(within(form).getByLabelText('Waiver gate scope'), {
      target: { value: 'target_resolution' },
    });
    fireEvent.change(within(form).getByLabelText('Waiver reason'), {
      target: { value: 'external_source_unavailable' },
    });
    fireEvent.change(within(form).getByLabelText('Waiver justification'), {
      target: { value: 'The external source is unavailable for this release window.' },
    });
    fireEvent.click(within(form).getByRole('button', { name: 'Record human waiver' }));

    await waitFor(() => expect(apiMock.createCodeTraceabilityWaiver).toHaveBeenCalledTimes(1));
    expect(apiMock.createCodeTraceabilityWaiver).toHaveBeenCalledWith(
      'board-1',
      {
        entity_type: 'card',
        entity_id: 'card-1',
        scope: 'target_resolution',
        reason_code: 'external_source_unavailable',
        justification: 'The external source is unavailable for this release window.',
      },
    );

    cleanup();
    permissionState.canCreateWaiver = false;
    permissionState.canClearWaiver = true;
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...projection,
      waivers: [{
        id: 'waiver-1',
        board_id: 'board-1',
        entity_type: 'card',
        entity_id: 'card-1',
        scope: 'target_resolution',
        reason_code: 'external_source_unavailable',
        justification: 'The external source is unavailable for this release window.',
        active: true,
        created_by: 'operator-1',
        created_at: '2026-08-09T14:00:00Z',
        cleared_by: null,
        cleared_at: null,
      }],
    });
    render(
      <ImplementationTargetsPanel
        boardId="board-1"
        subjectId="card-1"
        subjectVersion={4}
        specVersion={7}
      />,
    );

    expect(await screen.findByText('Human exception · not attestation. A waiver covers one explicit gate scope; it never claims that source was inspected, resolved or executed.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear waiver' }));
    await waitFor(() => expect(apiMock.clearCodeTraceabilityWaiver).toHaveBeenCalledWith(
      'board-1',
      'waiver-1',
    ));
  });

  it('appends a closed overlap acknowledgement and reuses the dependency flow', async () => {
    permissionState.canAcknowledgeOverlap = true;
    const onCreateDependency = vi.fn();
    render(
      <ImplementationTargetsPanel
        boardId="board-1"
        subjectId="card-1"
        subjectVersion={4}
        specVersion={7}
        onCreateDependency={onCreateDependency}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Create dependency' }));
    expect(onCreateDependency).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge overlap' }));
    fireEvent.change(screen.getByLabelText('Overlap disposition'), {
      target: { value: 'accepted_parallel' },
    });
    fireEvent.change(screen.getByLabelText('Overlap acknowledgement justification'), {
      target: { value: 'The tasks modify independent branches of the method.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Append acknowledgement' }));

    await waitFor(() => expect(apiMock.acknowledgeImplementationOverlap).toHaveBeenCalledTimes(1));
    expect(apiMock.acknowledgeImplementationOverlap).toHaveBeenCalledWith(
      'board-1',
      'card-1',
      {
        target_a_id: 'target-1',
        target_b_id: 'target-2',
        resolution_a_id: 'resolution-1',
        resolution_b_id: 'resolution-2',
        disposition: 'accepted_parallel',
        justification: 'The tasks modify independent branches of the method.',
      },
    );
  });

  it('removes already-open human mutation forms immediately when authority is lost', async () => {
    permissionState.canRevokeEvidence = true;
    permissionState.canCreateTarget = true;
    permissionState.canAcknowledgeOverlap = true;
    permissionState.canCreateWaiver = true;
    const panels = () => (
      <>
        <CodeEvidencePanel boardId="board-1" subjectId="ref-1" subjectVersion={3} />
        <ImplementationTargetsPanel
          boardId="board-1"
          subjectId="card-1"
          subjectVersion={4}
          specVersion={7}
        />
      </>
    );
    const { rerender } = render(panels());

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke evidence' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add semantic target' }));
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge overlap' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create human waiver' }));
    expect(screen.getByLabelText('Evidence revocation reason')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Add semantic target' })).toBeInTheDocument();
    expect(screen.getByLabelText('Overlap acknowledgement justification')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Create human waiver' })).toBeInTheDocument();

    permissionState.canRevokeEvidence = false;
    permissionState.canCreateTarget = false;
    permissionState.canAcknowledgeOverlap = false;
    permissionState.canCreateWaiver = false;
    rerender(panels());

    expect(screen.queryByRole('button', { name: 'Revoke evidence' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add semantic target' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge overlap' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create human waiver' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Evidence revocation reason')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Overlap acknowledgement justification')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Create human waiver' })).not.toBeInTheDocument();
  });

  it('owns Escape, traps focus and restores the nested modal opener', async () => {
    render(<NestedReceiptHarness />);
    const opener = screen.getByRole('button', { name: 'Open receipt detail' });
    opener.focus();
    fireEvent.click(opener);

    const receiptDialog = await screen.findByRole('dialog', {
      name: 'Investigation receipt',
    });
    const close = screen.getByRole('button', { name: 'Close receipt detail' });
    await waitFor(() => expect(close).toHaveFocus());

    const lastFocusable = screen.getByRole('button', { name: 'Copy payload digest' });
    lastFocusable.focus();
    fireEvent.keyDown(receiptDialog, { key: 'Tab' });
    expect(close).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Investigation receipt' })).not.toBeInTheDocument();
    });
    expect(screen.getByRole('dialog', { name: 'Parent modal' })).toBeInTheDocument();
    expect(opener).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByText('Parent closed')).toBeInTheDocument();
  });
});
