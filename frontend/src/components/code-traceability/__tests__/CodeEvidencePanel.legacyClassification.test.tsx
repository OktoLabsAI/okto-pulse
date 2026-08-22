import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  CodeTraceabilityEvidence,
  CodeTraceabilityProjection,
  LegacyEvidenceClassificationBatchRequest,
  LegacyEvidenceClassificationBatchResult,
  SourceContextEvidenceItemV2,
  SourceContextSummaryV2,
} from '@/types';
import { CodeEvidencePanel } from '../CodeEvidencePanel';

const apiMock = vi.hoisted(() => ({
  getCodeTraceabilityProjection: vi.fn(),
  classifyLegacyCodeEvidence: vi.fn(),
  revokeCodeEvidence: vi.fn(),
}));

const authorityState = vi.hoisted(() => ({
  canReadProjection: true,
  canClassifyLegacyEvidence: true,
  canRevokeEvidence: false,
  isLoading: false,
  error: null as Error | null,
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('../useCodeTraceabilityAuthority', () => ({
  useCodeTraceabilityAuthority: () => authorityState,
}));

const EMPTY_ROLE_COUNTS = {
  current_implementation_count: 0,
  existing_scaffold_count: 0,
  existing_constraint_count: 0,
  reference_pattern_count: 0,
  uncategorized_legacy_count: 0,
};

function evidence(id: string, claim: string): CodeTraceabilityEvidence {
  return {
    id,
    investigation_receipt_id: `receipt-${id}`,
    source_ref: 'repository:payments',
    parent_type: 'refinement',
    parent_id: 'refinement-1',
    parent_version: 4,
    evidence_type: 'code_observation',
    claim,
    selector_kind: 'relative_path',
    relative_path: `src/payments/${id}.ts`,
    language: 'typescript',
    symbol_kind: null,
    qualified_symbol: null,
    attestation_state: 'agent_attested',
    lifecycle_status: 'active',
    supersedes_evidence_id: null,
  };
}

function contextItem(
  evidenceId: string,
  overrides: Partial<SourceContextEvidenceItemV2> = {},
): SourceContextEvidenceItemV2 {
  return {
    evidence_id: evidenceId,
    source_role: 'uncategorized_legacy',
    relevance_summary: null,
    scope_relation: null,
    source_origin: null,
    interpretation_limit: null,
    baseline_provenance: {
      presence: 'committed_snapshot',
      workspace_state_id: `workspace-${evidenceId}`,
      provenance_note: null,
    },
    context_origin: 'unclassified_legacy',
    context_contract_version: null,
    evidence_applicable: null,
    ...overrides,
  };
}

function sourceContext(
  uncategorizedCount: number,
  currentImplementationCount = 0,
): SourceContextSummaryV2 {
  return {
    delivery_context: 'brownfield',
    delivery_context_provenance: {
      value: 'brownfield',
      source_refinement_id: 'refinement-1',
      source_refinement_version: 4,
    },
    investigation_outcome: 'evidence_applicable',
    role_counts: {
      ...EMPTY_ROLE_COUNTS,
      current_implementation_count: currentImplementationCount,
      uncategorized_legacy_count: uncategorizedCount,
    },
    classification_state: {
      classified_count: currentImplementationCount,
      uncategorized_legacy_count: uncategorizedCount,
    },
    evidence_applicable: uncategorizedCount > 0 ? null : true,
    interpretation_rule: 'Only classified current implementation supports implementation coverage.',
    items_not_current_implementation_count: uncategorizedCount,
    technical_details_available: true,
  };
}

function projection({
  evidenceItems,
  contextItems,
  classificationInputIds,
  subjectVersion = 4,
}: {
  evidenceItems: CodeTraceabilityEvidence[];
  contextItems: SourceContextEvidenceItemV2[];
  classificationInputIds: string[];
  subjectVersion?: number;
}): CodeTraceabilityProjection {
  const uncategorizedCount = contextItems.filter(
    (item) => item.source_role === 'uncategorized_legacy',
  ).length;
  const currentImplementationCount = contextItems.filter(
    (item) => item.source_role === 'current_implementation',
  ).length;
  return {
    subject_type: 'refinement',
    subject_id: 'refinement-1',
    subject_version: subjectVersion,
    profile: 'detail',
    context_scope: 'default',
    source_context: sourceContext(uncategorizedCount, currentImplementationCount),
    source_context_items: contextItems,
    source_context_classification_inputs: classificationInputIds.map((evidenceId) => ({
      evidence_id: evidenceId,
      expected_evidence_payload_sha256: evidenceId.charCodeAt(evidenceId.length - 1)
        .toString(16)
        .padStart(64, '0'),
      expected_classification_revision: contextItems.find(
        (item) => item.evidence_id === evidenceId,
      )?.classification_revision ?? 0,
      baseline_provenance: {
        presence: 'committed_snapshot',
        workspace_state_id: `workspace-${evidenceId}`,
        provenance_note: null,
        provenance_note_required: false,
      },
    })),
    contextual_evidence_coverage: {
      total: currentImplementationCount,
      linked: currentImplementationCount,
      dispositioned: 0,
      pending: 0,
      pending_ids: [],
      unresolved_applicability_count: uncategorizedCount,
      coverage_pct: uncategorizedCount > 0 ? null : 100,
      projection_complete: uncategorizedCount === 0,
    },
    evidence: evidenceItems,
    inherited_evidence_ids: evidenceItems.map((item) => item.id),
    direct_evidence_ids: [],
    referenced_evidence_ids: [],
    links: [],
    dispositions: [],
    targets: [],
    resolutions: [],
    overlaps: [],
    waivers: [],
    heads: [],
    counts: { evidence: evidenceItems.length },
    coverage: {
      total: evidenceItems.length,
      linked: 0,
      dispositioned: 0,
      pending: evidenceItems.length,
      pending_ids: evidenceItems.map((item) => item.id),
      coverage_pct: 0,
    },
    resolution_freshness: {},
    gate_readiness: {
      mode: 'advisory',
      allowed: true,
      passed: true,
      blockers: [],
      receipt_currentness: {},
      resolution_freshness: {},
    },
  };
}

const classificationResult: LegacyEvidenceClassificationBatchResult = {
  batch_id: 'batch-1',
  board_id: 'board-1',
  classified_by: 'operator-1',
  classified_at: '2026-08-22T20:00:00Z',
  request_sha256: 'f'.repeat(64),
  classifications: [],
  replayed: false,
};

function panel() {
  return (
    <CodeEvidencePanel
      boardId="board-1"
      subjectId="refinement-1"
      subjectVersion={4}
    />
  );
}

function fillDrawerItem(index: number) {
  fireEvent.click(screen.getByTestId(`legacy-classification-item-${index}`));
  fireEvent.click(within(
    screen.getByRole('group', { name: `Source role for evidence ${index}` }),
  ).getByRole('radio', { name: /^Existing implementation/ }));
  fireEvent.change(screen.getByLabelText(`Relevance summary for evidence ${index}`), {
    target: { value: `Relevant implementation ${index}` },
  });
  fireEvent.change(screen.getByLabelText(`Scope relation for evidence ${index}`), {
    target: { value: `Delivery relation ${index}` },
  });
  fireEvent.change(screen.getByLabelText(`Source origin for evidence ${index}`), {
    target: { value: `Repository baseline ${index}` },
  });
}

describe('CodeEvidencePanel legacy classification integration', () => {
  beforeEach(() => {
    Object.assign(authorityState, {
      canReadProjection: true,
      canClassifyLegacyEvidence: true,
      canRevokeEvidence: false,
      isLoading: false,
      error: null,
    });
    apiMock.getCodeTraceabilityProjection.mockReset();
    apiMock.classifyLegacyCodeEvidence.mockReset();
    apiMock.revokeCodeEvidence.mockReset();
    apiMock.classifyLegacyCodeEvidence.mockResolvedValue(classificationResult);
  });

  afterEach(() => cleanup());

  it.each([
    ['missing canonical inputs', true, 4, []],
    ['historical Refinement projection', true, 3, ['evidence-1']],
    ['missing classify authority', false, 4, ['evidence-1']],
  ] as const)(
    'ts_f817bea4 — hides the primary action for %s',
    async (_case, canClassify, subjectVersion, classificationInputIds) => {
      authorityState.canClassifyLegacyEvidence = canClassify;
      apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
        evidenceItems: [evidence('evidence-1', 'Legacy payment implementation.')],
        contextItems: [contextItem('evidence-1')],
        classificationInputIds: [...classificationInputIds],
        subjectVersion,
      }));

      render(panel());

      await screen.findByTestId('refinement-code-evidence-panel');
      await waitFor(() => expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(1));
      expect(screen.queryByRole('button', { name: 'Review unclassified Evidence' }))
        .not.toBeInTheDocument();
    },
  );

  it('ts_f817bea4 — routes one primary action to every and only unclassified canonical input', async () => {
    const initial = projection({
      evidenceItems: [
        evidence('evidence-1', 'First legacy implementation.'),
        evidence('evidence-2', 'Second legacy implementation.'),
        evidence('evidence-3', 'Previously classified legacy implementation.'),
      ],
      contextItems: [
        contextItem('evidence-1'),
        contextItem('evidence-2'),
        contextItem('evidence-3', {
          source_role: 'existing_constraint',
          relevance_summary: 'The existing boundary constrains this delivery.',
          scope_relation: 'Same bounded scope.',
          source_origin: 'Repository baseline.',
          context_origin: 'human_legacy_classification',
          context_contract_version: 2,
          evidence_applicable: false,
          classification_revision: 1,
        }),
      ],
      classificationInputIds: ['evidence-1', 'evidence-2', 'evidence-3'],
    });
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(initial);
    render(panel());

    const opener = await screen.findByRole('button', { name: 'Review unclassified Evidence' });
    expect(screen.getAllByRole('button', { name: 'Review unclassified Evidence' })).toHaveLength(1);
    expect(opener.closest('[data-testid="source-context-overview"]')).not.toBeNull();
    expect(screen.getByText('Their original Evidence is preserved. Choose what each observation means before using it for delivery decisions.'))
      .toBeInTheDocument();
    opener.focus();
    fireEvent.click(opener);

    expect(await screen.findByRole('dialog', { name: 'Classify legacy Evidence' }))
      .toBeInTheDocument();
    const drawer = screen.getByRole('dialog', { name: 'Classify legacy Evidence' });
    expect(within(drawer).getAllByRole('group', { name: /Source role for evidence/ }))
      .toHaveLength(1);
    expect(within(drawer).getByTestId('legacy-classification-item-1'))
      .toHaveAttribute('aria-current', 'step');
    expect(within(drawer).getByTestId('legacy-classification-item-2'))
      .not.toHaveAttribute('aria-current');
    expect(within(drawer).getAllByText('First legacy implementation.').length).toBeGreaterThan(0);
    expect(within(drawer).getByText('Second legacy implementation.')).toBeInTheDocument();
    expect(within(drawer).queryByText('Previously classified legacy implementation.'))
      .not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(opener).toHaveFocus());
    expect(apiMock.classifyLegacyCodeEvidence).not.toHaveBeenCalled();
    expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(1);
  });

  it('ts_15a6a9dc / ts_bd449ee2 — applies one atomic batch, refetches once and publishes one coherent live and focused result', async () => {
    const evidenceItems = [
      evidence('evidence-1', 'Original first Evidence claim.'),
      evidence('evidence-2', 'Original second Evidence claim.'),
    ];
    const initial = projection({
      evidenceItems,
      contextItems: [contextItem('evidence-1'), contextItem('evidence-2')],
      classificationInputIds: ['evidence-1', 'evidence-2'],
    });
    const refreshed = projection({
      evidenceItems,
      contextItems: [
        contextItem('evidence-1', {
          source_role: 'current_implementation',
          relevance_summary: 'Relevant implementation 1',
          scope_relation: 'Delivery relation 1',
          source_origin: 'Repository baseline 1',
          context_origin: 'human_legacy_classification',
          context_contract_version: 2,
          evidence_applicable: true,
          classification_revision: 1,
        }),
        contextItem('evidence-2', {
          source_role: 'current_implementation',
          relevance_summary: 'Relevant implementation 2',
          scope_relation: 'Delivery relation 2',
          source_origin: 'Repository baseline 2',
          context_origin: 'human_legacy_classification',
          context_contract_version: 2,
          evidence_applicable: true,
          classification_revision: 1,
        }),
      ],
      classificationInputIds: ['evidence-1', 'evidence-2'],
    });
    apiMock.getCodeTraceabilityProjection
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(refreshed);
    render(panel());

    fireEvent.click(await screen.findByRole('button', { name: 'Review unclassified Evidence' }));
    fillDrawerItem(1);
    fillDrawerItem(2);
    fireEvent.change(screen.getByLabelText('Classification justification'), {
      target: { value: 'Reviewed both legacy records against the frozen baseline.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review batch' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply classification' }));

    await waitFor(() => expect(apiMock.classifyLegacyCodeEvidence).toHaveBeenCalledTimes(1));
    const [boardId, request, signal] = apiMock.classifyLegacyCodeEvidence.mock.calls[0] as [
      string,
      LegacyEvidenceClassificationBatchRequest,
      AbortSignal,
    ];
    expect(boardId).toBe('board-1');
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(request.items.map((item) => item.evidence_id)).toEqual(['evidence-1', 'evidence-2']);
    expect(request.justification).toBe(
      'Reviewed both legacy records against the frozen baseline.',
    );

    const success = await screen.findByText('2 Evidence classifications updated.');
    expect(success).toHaveAttribute('role', 'status');
    expect(success).toHaveTextContent('2 Evidence classifications updated.');
    await waitFor(() => expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('dialog', { name: 'Classify legacy Evidence' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review unclassified Evidence' }))
      .not.toBeInTheDocument();
    expect(screen.queryByText(/legacy items need human classification/i)).not.toBeInTheDocument();
    expect(screen.getByText('Original first Evidence claim.')).toBeInTheDocument();
    expect(screen.getByText('Original second Evidence claim.')).toBeInTheDocument();
    expect(screen.getAllByText('Existing implementation').length).toBeGreaterThan(0);
    await waitFor(() => expect(success).toHaveFocus());
  });

  it('ts_f817bea4 / ts_4822298b — exposes role-preserving reclassification only for legacy Evidence inside technical details', async () => {
    const legacy = evidence('evidence-1', 'Classified legacy Evidence.');
    const native = evidence('evidence-2', 'Native V2 Evidence.');
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      evidenceItems: [legacy, native],
      contextItems: [
        contextItem(legacy.id, {
          source_role: 'current_implementation',
          relevance_summary: 'Current legacy relevance.',
          scope_relation: 'Current legacy relation.',
          source_origin: 'Human-reviewed legacy origin.',
          context_origin: 'human_legacy_classification',
          context_contract_version: 2,
          evidence_applicable: true,
          classification_revision: 2,
        }),
        contextItem(native.id, {
          source_role: 'current_implementation',
          relevance_summary: 'Native V2 relevance.',
          scope_relation: 'Native V2 relation.',
          source_origin: 'Authored V2 origin.',
          context_origin: 'authored',
          context_contract_version: 2,
          evidence_applicable: true,
        }),
      ],
      // An adversarial native input proves origin, not mere input presence, gates the action.
      classificationInputIds: [legacy.id, native.id],
    }));
    render(panel());

    const legacyArticle = (await screen.findByRole('heading', {
      name: 'Classified legacy Evidence.',
    })).closest('article') as HTMLElement;
    const nativeArticle = screen.getByRole('heading', { name: 'Native V2 Evidence.' })
      .closest('article') as HTMLElement;
    fireEvent.click(within(legacyArticle).getByText('Technical evidence details'));
    const change = within(legacyArticle).getByRole('button', { name: 'Change classification' });
    fireEvent.click(within(nativeArticle).getByText('Technical evidence details'));
    expect(within(nativeArticle).queryByRole('button', { name: 'Change classification' }))
      .not.toBeInTheDocument();

    change.focus();
    fireEvent.click(change);
    const roleGroup = await screen.findByRole('group', { name: 'Source role for evidence 1' });
    expect(within(roleGroup).getByRole('radio', { name: /^Existing implementation/ }))
      .toBeChecked();
    expect(screen.getAllByRole('group', { name: /Source role for evidence/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(change).toHaveFocus());
  });

  it('ts_f817bea4 — closes and clears an open draft immediately when classify authority is lost', async () => {
    const initial = projection({
      evidenceItems: [evidence('evidence-1', 'Permission-sensitive legacy Evidence.')],
      contextItems: [contextItem('evidence-1')],
      classificationInputIds: ['evidence-1'],
    });
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(initial);
    const view = render(panel());

    fireEvent.click(await screen.findByRole('button', { name: 'Review unclassified Evidence' }));
    fireEvent.change(screen.getByLabelText('Relevance summary for evidence 1'), {
      target: { value: 'Draft that must be cleared.' },
    });
    authorityState.canClassifyLegacyEvidence = false;
    view.rerender(panel());

    await waitFor(() => expect(
      screen.queryByRole('dialog', { name: 'Classify legacy Evidence' }),
    ).not.toBeInTheDocument());
    expect(screen.queryByDisplayValue('Draft that must be cleared.')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review unclassified Evidence' }))
      .not.toBeInTheDocument();
    expect(apiMock.classifyLegacyCodeEvidence).not.toHaveBeenCalled();
  });
});
