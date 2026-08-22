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

const projectionReadLeaves = new Set([
  'code_traceability.investigation.read',
  'code_traceability.evidence.read',
  'code_traceability.target.read',
  'code_traceability.overlap.read',
]);

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    has: (flag: string) => (
      projectionReadLeaves.has(flag)
      || (flag === 'code_traceability.investigation.revoke' && permissionState.canRevoke)
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

const gateProjection: CodeTraceabilityProjection = {
  ...projection,
  subject_type: 'spec',
  subject_id: 'spec-1',
  subject_version: 7,
  profile: 'full',
  context_scope: 'gate',
  source_context: {
    delivery_context: 'brownfield',
    delivery_context_provenance: null,
    investigation_outcome: 'evidence_applicable',
    role_counts: {
      current_implementation_count: 1,
      existing_scaffold_count: 0,
      existing_constraint_count: 0,
      reference_pattern_count: 0,
      uncategorized_legacy_count: 0,
    },
    classification_state: {
      classified_count: 1,
      uncategorized_legacy_count: 0,
    },
    evidence_applicable: true,
    interpretation_rule: 'Only current implementation evidence contributes to coverage.',
    items_not_current_implementation_count: 0,
    technical_details_available: true,
  },
  source_context_items: [{
    evidence_id: 'evidence-1',
    source_role: 'current_implementation',
    relevance_summary: 'Current implementation behavior.',
    scope_relation: 'Directly implements the refinement scope.',
    source_origin: 'Existing repository.',
    interpretation_limit: null,
    baseline_provenance: null,
    context_origin: 'authored',
    context_contract_version: 2,
    evidence_applicable: true,
    classification_revision: null,
    classification_sha256: null,
  }],
  contextual_evidence_coverage: {
    total: 1,
    linked: 1,
    dispositioned: 0,
    pending: 0,
    pending_ids: [],
    unresolved_applicability_count: 0,
    coverage_pct: 100,
    projection_complete: true,
  },
  obligation_evidence_mappings: [{
    link_id: 'link-1',
    evidence_id: 'evidence-1',
    obligation_type: 'technical_requirement',
    obligation_id: 'TR-2',
    obligation_ref: 'technical_requirement:TR-2',
    relation_type: 'supports',
    evidence_applicable: true,
    context_origin: 'authored',
    source_role: 'current_implementation',
  }],
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
  apiMock.getCodeTraceabilityProjection.mockImplementation(
    async (_boardId: string, subjectType: string) => (
      subjectType === 'spec' ? gateProjection : projection
    ),
  );
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

  it('ts_ddfe595b — renders separate authoritative Brownfield coverage fields and human obligation titles', async () => {
    const matrixProjection: CodeTraceabilityProjection = {
      ...gateProjection,
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
        total: 99,
        linked: 99,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        coverage_pct: 100,
      },
      source_context_items: [
        ...(gateProjection.source_context_items ?? []),
        {
          ...(gateProjection.source_context_items ?? [])[0],
          evidence_id: 'evidence-2',
        },
      ],
      contextual_evidence_coverage: {
        total: 2,
        linked: 1,
        dispositioned: 0,
        pending: 1,
        pending_ids: ['evidence-2'],
        unresolved_applicability_count: 0,
        coverage_pct: 37.5,
        projection_complete: true,
      },
      obligation_evidence_mappings: [
        {
          ...(gateProjection.obligation_evidence_mappings ?? [])[0],
          link_id: 'link-ir',
          obligation_type: 'integration_requirement',
          obligation_id: 'IR-7',
          obligation_ref: 'integration_requirement:IR-7',
        },
        {
          ...(gateProjection.obligation_evidence_mappings ?? [])[0],
          link_id: 'link-or',
          obligation_type: 'observability_requirement',
          obligation_id: 'OR-3',
          obligation_ref: 'observability_requirement:OR-3',
        },
      ],
      gate_readiness: {
        ...gateProjection.gate_readiness,
        receipt_currentness: {
          'receipt-1': 'current',
          'receipt-2': 'expired',
        },
      },
    };
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(matrixProjection);

    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        obligationTitles={{
          'IR-7': 'IR-1: Payment provider contract',
          'OR-3': 'OR-1: Checkout failure alert',
        }}
      />,
    );

    const coverageRegion = await screen.findByRole('region', { name: 'Code Evidence coverage' });
    expect(within(coverageRegion).getByTestId('contextual-evidence-total')).toHaveTextContent('2');
    expect(within(coverageRegion).getByTestId('contextual-evidence-linked')).toHaveTextContent('1');
    expect(within(coverageRegion).getByTestId('contextual-evidence-dispositioned')).toHaveTextContent('0');
    expect(within(coverageRegion).getByTestId('contextual-evidence-pending')).toHaveTextContent('1');
    expect(within(coverageRegion).getByTestId('contextual-evidence-coverage-pct')).toHaveTextContent('37.5%');
    expect(screen.getByTestId('code-evidence-coverage-status')).toHaveTextContent('Pending');
    expect(within(coverageRegion).queryByText('evidence items addressed')).not.toBeInTheDocument();
    expect(within(coverageRegion).queryByText('1/2')).not.toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'IR' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'OR' })).toBeInTheDocument();
    expect(screen.getByText('IR-1: Payment provider contract')).toBeInTheDocument();
    expect(screen.getByText('OR-1: Checkout failure alert')).toBeInTheDocument();
    expect(screen.queryByText('IR-7')).not.toBeInTheDocument();
    expect(screen.queryByText('OR-3')).not.toBeInTheDocument();
    expect(screen.getByText('Current receipt')).toBeInTheDocument();
    expect(screen.getByText('Expired')).toBeInTheDocument();
    expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledWith(
      'board-1',
      'spec',
      'spec-1',
      7,
      {
        profile: 'full',
        signal: expect.any(AbortSignal),
        contextScope: 'gate',
      },
    );
  });

  it('ts_07ea0cf3 — never aggregates contextual denominator fields from Evidence, links, mappings, or legacy coverage', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...gateProjection,
      evidence: [projection.evidence[0]],
      inherited_evidence_ids: ['evidence-1'],
      coverage: {
        total: 99,
        linked: 99,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        coverage_pct: 100,
      },
      contextual_evidence_coverage: {
        total: 7,
        linked: 2,
        dispositioned: 3,
        pending: 2,
        pending_ids: ['evidence-pending-a', 'evidence-pending-b'],
        unresolved_applicability_count: 0,
        coverage_pct: 42.75,
        projection_complete: true,
      },
      links: [{
        ...projection.links[0],
        id: 'legacy-link-only',
        evidence_id: 'evidence-1',
      }],
      obligation_evidence_mappings: [{
        ...(gateProjection.obligation_evidence_mappings ?? [])[0],
        link_id: 'mapping-only',
        evidence_id: 'evidence-1',
        obligation_type: 'technical_requirement',
        obligation_id: 'TR-2',
        obligation_ref: 'technical_requirement:TR-2',
      }],
    });

    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        obligationTitles={{ 'TR-2': 'TR-1: Acquire the lock before persistence' }}
      />,
    );

    const coverageRegion = await screen.findByRole('region', { name: 'Code Evidence coverage' });
    expect(within(coverageRegion).getByTestId('contextual-evidence-total')).toHaveTextContent('7');
    expect(within(coverageRegion).getByTestId('contextual-evidence-linked')).toHaveTextContent('2');
    expect(within(coverageRegion).getByTestId('contextual-evidence-dispositioned')).toHaveTextContent('3');
    expect(within(coverageRegion).getByTestId('contextual-evidence-pending')).toHaveTextContent('2');
    expect(within(coverageRegion).getByTestId('contextual-evidence-coverage-pct')).toHaveTextContent('42.75%');
    expect(within(coverageRegion).queryByText('5/7')).not.toBeInTheDocument();
    expect(within(coverageRegion).queryByText('99')).not.toBeInTheDocument();
    expect(within(coverageRegion).queryByText('100%')).not.toBeInTheDocument();
    expect(screen.getByText('TR-1: Acquire the lock before persistence')).toBeInTheDocument();
    expect(screen.queryByText('TR-2')).not.toBeInTheDocument();
  });

  it('never presents a truncated gate projection as covered or skippable', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...gateProjection,
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
        ...gateProjection.gate_readiness,
        blockers: [{
          code: 'code_traceability_projection_incomplete',
          message: 'Gate context exceeded a server-owned projection budget.',
          blocking: false,
        }],
      },
      contextual_evidence_coverage: {
        ...gateProjection.contextual_evidence_coverage!,
        projection_complete: false,
        coverage_pct: null,
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
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.getByText('Visible counts are lower bounds. Refresh or narrow the projection.'))
      .toBeInTheDocument();
  });

  it.each([
    { profile: 'detail' as const, contextScope: 'gate' as const },
    { profile: 'full' as const, contextScope: 'default' as const },
  ])('does not replace the server-owned contextual aggregate for $profile + $contextScope', async ({
    profile,
    contextScope,
  }) => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...gateProjection,
      profile,
      context_scope: contextScope,
    });

    render(
      <EvidenceMatrixPanel boardId="board-1" subjectId="spec-1" subjectVersion={7} />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status')).toHaveTextContent('Covered');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('ts_47048de6 — renders the exact server-owned zero-applicable Matrix without coverage artifacts', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...gateProjection,
      evidence: [],
      inherited_evidence_ids: [],
      links: [],
      obligation_evidence_mappings: [],
      source_context_items: [],
      source_context: {
        ...gateProjection.source_context!,
        delivery_context: 'greenfield',
        investigation_outcome: 'no_relevant_existing_implementation',
        evidence_applicable: false,
      },
      contextual_evidence_coverage: {
        total: 0,
        linked: 0,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        unresolved_applicability_count: 0,
        coverage_pct: null,
        projection_complete: true,
      },
      coverage: {
        total: 9,
        linked: 9,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        coverage_pct: 100,
      },
    });

    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        boardSkipCoverage
        skipCoverage
        canEditCoverageFlags
        onSkipCoverageChange={vi.fn()}
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status'))
      .toHaveTextContent('Not applicable');
    expect(screen.getByText('No relevant existing implementation was found for this delivery context.'))
      .toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.queryByTestId('contextual-evidence-total')).not.toBeInTheDocument();
    expect(screen.queryByTestId('contextual-evidence-linked')).not.toBeInTheDocument();
    expect(screen.queryByTestId('contextual-evidence-dispositioned')).not.toBeInTheDocument();
    expect(screen.queryByTestId('contextual-evidence-pending')).not.toBeInTheDocument();
    expect(screen.queryByTestId('contextual-evidence-coverage-pct')).not.toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: 'Skip Code Evidence coverage' }))
      .not.toBeInTheDocument();
    expect(screen.queryByTestId('code-evidence-board-skip-notice')).not.toBeInTheDocument();
    expect(screen.queryByText(/no inherited code evidence/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/waiver|warning/i)).not.toBeInTheDocument();
  });

  it('does not treat raw legacy links as coverage while applicability is unresolved', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...gateProjection,
      source_context: {
        ...gateProjection.source_context!,
        delivery_context: 'hybrid',
        evidence_applicable: null,
        investigation_outcome: null,
      },
      source_context_items: [{
        ...gateProjection.source_context_items![0],
        source_role: 'uncategorized_legacy',
        context_origin: 'unclassified_legacy',
        evidence_applicable: null,
        context_contract_version: null,
      }],
      contextual_evidence_coverage: {
        ...gateProjection.contextual_evidence_coverage!,
        linked: 0,
        pending: 0,
        pending_ids: [],
        unresolved_applicability_count: 1,
        coverage_pct: null,
      },
      obligation_evidence_mappings: [{
        ...gateProjection.obligation_evidence_mappings![0],
        evidence_applicable: null,
        context_origin: 'unclassified_legacy',
        source_role: 'uncategorized_legacy',
      }],
    });

    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        obligationTitles={{ 'TR-2': 'TR-1: Acquire the lock before persistence' }}
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status'))
      .toHaveTextContent('Needs classification');
    expect(screen.getAllByText('Needs classification', { selector: 'span' })).toHaveLength(2);
    expect(screen.queryByText('TR-1: Acquire the lock before persistence')).not.toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
  });

  it('excludes direct or future evidence outside the authoritative inherited snapshot', async () => {
    const inheritedProjection: CodeTraceabilityProjection = {
      ...gateProjection,
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

    const coverageRegion = await screen.findByRole('region', { name: 'Code Evidence coverage' });
    expect(within(coverageRegion).getByTestId('contextual-evidence-total')).toHaveTextContent('1');
    expect(within(coverageRegion).getByTestId('contextual-evidence-linked')).toHaveTextContent('1');
    expect(within(coverageRegion).getByTestId('contextual-evidence-dispositioned')).toHaveTextContent('0');
    expect(within(coverageRegion).getByTestId('contextual-evidence-pending')).toHaveTextContent('0');
    expect(within(coverageRegion).getByTestId('contextual-evidence-coverage-pct')).toHaveTextContent('100%');
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

  it('reloads the projection when a same-version local skip is turned off', async () => {
    apiMock.getCodeTraceabilityProjection
      .mockResolvedValueOnce({
        ...gateProjection,
        coverage: {
          ...gateProjection.coverage,
          skipped: true,
        },
        gate_readiness: {
          ...gateProjection.gate_readiness,
          evidence_coverage_skipped: true,
        },
      })
      .mockResolvedValueOnce({
        ...gateProjection,
        coverage: {
          ...gateProjection.coverage,
          skipped: false,
        },
        gate_readiness: {
          ...gateProjection.gate_readiness,
          evidence_coverage_skipped: false,
        },
      });

    const { rerender } = render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        skipCoverage
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status'))
      .toHaveTextContent('Skipped for this Spec');
    expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(1);

    rerender(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        skipCoverage={false}
      />,
    );

    await waitFor(() => {
      expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(2);
      expect(screen.getByTestId('code-evidence-coverage-status'))
        .toHaveTextContent('Covered');
    });
    expect(apiMock.getCodeTraceabilityProjection.mock.calls[1]?.[3]).toBe(7);
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

  it('distinguishes a Board-wide skip from the local Spec override', async () => {
    const onSkipCoverageChange = vi.fn().mockResolvedValue(undefined);
    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
        boardSkipCoverage
        skipCoverage={false}
        canEditCoverageFlags
        onSkipCoverageChange={onSkipCoverageChange}
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status'))
      .toHaveTextContent('Skipped by Board');
    expect(screen.getByTestId('code-evidence-board-skip-notice')).toHaveTextContent(
      'skipped for every Spec by the Board setting',
    );
    const localToggle = screen.getByRole('switch', {
      name: 'Skip Code Evidence coverage',
    });
    expect(localToggle).toHaveAttribute('aria-checked', 'false');
    expect(localToggle).toBeEnabled();

    fireEvent.click(localToggle);
    await waitFor(() => expect(onSkipCoverageChange).toHaveBeenCalledWith(true));
  });

  it('honors the authoritative effective-skip projection without inventing a local override', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue({
      ...gateProjection,
      coverage: {
        ...gateProjection.coverage,
        skipped: true,
      },
      gate_readiness: {
        ...gateProjection.gate_readiness,
        evidence_coverage_skipped: true,
      },
    });

    render(
      <EvidenceMatrixPanel
        boardId="board-1"
        subjectId="spec-1"
        subjectVersion={7}
      />,
    );

    expect(await screen.findByTestId('code-evidence-coverage-status'))
      .toHaveTextContent('Skipped');
    expect(screen.queryByTestId('code-evidence-board-skip-notice')).not.toBeInTheDocument();
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
