import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  getSpecChecklistState: vi.fn(),
  listChecklistTemplates: vi.fn(),
  listChecklistExecutions: vi.fn(),
  startChecklistExecution: vi.fn(),
  submitChecklistExecution: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));

import { SpecChecklistPanel } from '../SpecChecklistPanel';
import { SubmitSpecValidationModal } from '../SubmitSpecValidationModal';

const itemIds = [
  'chk_scope_boundaries',
  'chk_fr_value',
  'chk_fr_ac_testable',
  'chk_ac_measurable',
  'chk_edge_failures',
  'chk_dependencies_assumptions',
  'chk_no_placeholders',
  'chk_fr_tr_separation',
  'chk_ids_traceability',
  'chk_decisions_rationale',
];

const template = {
  template_id: 'specify',
  version: '/specify/v1' as const,
  digest: 'b'.repeat(64),
  items: itemIds.map((itemId, index) => ({
    item_id: itemId,
    title_en: `Checklist item ${index + 1}`,
    title_pt: `Item ${index + 1}`,
    description_en: `Description ${index + 1}`,
    description_pt: `Descrição ${index + 1}`,
    allow_na: index >= 4,
  })),
};

const binding = {
  id: 'a'.repeat(64),
  board_id: 'board-1',
  target_type: 'spec' as const,
  phase: 'spec_validation' as const,
  mode: 'blocking' as const,
  version: 2,
  expected_revision: 2,
  digest: 'a'.repeat(64),
  template_version_id: '/specify/v1' as const,
};

const incompleteState = {
  status: 'not_started' as const,
  subject: {
    board_id: 'board-1',
    spec_id: 'spec-1',
    spec_version: 4,
    content_digest: 'c'.repeat(64),
    input_digest: 'd'.repeat(64),
    status: 'approved',
    archived: false,
  },
  binding,
  current_receipt: null,
  currentness: null,
  gate: {
    mode: 'blocking' as const,
    allowed: false,
    reason: 'checklist_receipt_required',
    currentness: null,
  },
};

const receiptResults = itemIds.map((itemId) => ({
  item_id: itemId,
  outcome: 'pass' as const,
  anchor: `anchor:${itemId}`,
  rationale: null,
}));

const currentReceipt = {
  id: 'receipt-1',
  board_id: 'board-1',
  spec_id: 'spec-1',
  spec_version: 4,
  content_digest: 'c'.repeat(64),
  input_digest: 'd'.repeat(64),
  template_version_id: '/specify/v1' as const,
  template_digest: template.digest,
  binding_version: 2,
  binding_id: binding.id,
  binding_mode: 'blocking' as const,
  source: 'native' as const,
  request_digest: 'e'.repeat(64),
  head_revision: 1,
  predecessor_receipt_id: null,
  created_by: 'user-1',
  created_at: '2026-07-27T12:00:00Z',
  results: receiptResults,
  blocking_satisfied: true,
};

const currentState = {
  ...incompleteState,
  status: 'current' as const,
  current_receipt: currentReceipt,
  currentness: { current: true, stale_reasons: [] },
  gate: {
    mode: 'blocking' as const,
    allowed: true,
    reason: 'checklist_satisfied',
    currentness: { current: true, stale_reasons: [] },
  },
};

describe('SpecChecklistPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getSpecChecklistState.mockResolvedValue(incompleteState);
    apiMock.listChecklistTemplates.mockResolvedValue({
      items: [template],
      total: 1,
    });
    apiMock.listChecklistExecutions.mockResolvedValue({
      items: [],
      total_filtered: 0,
      total_overall: 0,
      offset: 0,
      limit: 25,
    });
    apiMock.startChecklistExecution.mockResolvedValue({
      execution_id: 'execution-1',
      items: template.items,
      subject_digest: 'd'.repeat(64),
      template_digest: template.digest,
    });
    apiMock.submitChecklistExecution.mockResolvedValue({
      receipt_id: 'receipt-1',
      outcome: 'pass',
      head_revision: 1,
    });
  });

  it('shows all immutable checklist items on a draft before execution starts', async () => {
    apiMock.getSpecChecklistState.mockResolvedValue({
      ...incompleteState,
      subject: {
        ...incompleteState.subject,
        status: 'draft',
      },
    });

    render(
      <SpecChecklistPanel
        boardId="board-1"
        specId="spec-1"
        expectedSpecVersion={4}
      />,
    );

    const preview = await screen.findByTestId('checklist-template-preview');
    expect(within(preview).getAllByText(/Checklist item \d+/)).toHaveLength(10);
    expect(within(preview).getAllByText('Not evaluated')).toHaveLength(10);
    expect(
      within(preview).queryByPlaceholderText(
        'Required evidence anchor (FR/AC/section)',
      ),
    ).not.toBeInTheDocument();
  });

  it('keeps all persisted outcomes, anchors and rationales visible after submit', async () => {
    apiMock.getSpecChecklistState.mockResolvedValue({
      ...currentState,
      current_receipt: {
        ...currentReceipt,
        results: receiptResults.map((result, index) => ({
          ...result,
          rationale: index === 0 ? 'Verified against the scope section.' : null,
        })),
      },
    });

    render(
      <SpecChecklistPanel
        boardId="board-1"
        specId="spec-1"
        expectedSpecVersion={4}
      />,
    );

    const persisted = await screen.findByTestId('checklist-receipt-results');
    expect(within(persisted).getAllByText(/Checklist item \d+/)).toHaveLength(10);
    expect(within(persisted).getAllByText('pass')).toHaveLength(10);
    expect(within(persisted).getByText('anchor:chk_scope_boundaries')).toBeInTheDocument();
    expect(
      within(persisted).getByText('Verified against the scope section.'),
    ).toBeInTheDocument();
  });

  it('submits exactly the ten ordered template items with required anchors', async () => {
    render(
      <SpecChecklistPanel
        boardId="board-1"
        specId="spec-1"
        expectedSpecVersion={4}
      />,
    );

    expect(await screen.findByTestId('checklist-state-status')).toHaveTextContent(
      'not_started',
    );
    expect(screen.getByText('checklist_receipt_required')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Start checklist' }));
    await screen.findByTestId('checklist-execution-form');

    const outcomes = screen.getAllByRole('combobox');
    const anchors = screen.getAllByPlaceholderText(
      'Required evidence anchor (FR/AC/section)',
    );
    expect(outcomes).toHaveLength(10);
    expect(anchors).toHaveLength(10);

    outcomes.forEach((select) =>
      fireEvent.change(select, { target: { value: 'pass' } }),
    );
    anchors.forEach((input, index) =>
      fireEvent.change(input, { target: { value: `FR-${index + 1}` } }),
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Submit complete checklist' }),
    );

    await waitFor(() =>
      expect(apiMock.submitChecklistExecution).toHaveBeenCalledTimes(1),
    );
    const payload = apiMock.submitChecklistExecution.mock.calls[0][3];
    expect(payload.results).toHaveLength(10);
    expect(payload.results.map((item: { item_id: string }) => item.item_id)).toEqual(
      itemIds,
    );
    expect(
      payload.results.every((item: { anchor: string }) => item.anchor.length > 0),
    ).toBe(true);
  });

  it('keeps Spec Validation fail-closed until a blocking receipt is current', async () => {
    render(
      <SubmitSpecValidationModal
        specId="spec-1"
        specTitle="Checklist-gated Spec"
        boardId="board-1"
        specVersion={4}
        settings={{
          max_scenarios_per_card: 3,
          skip_test_coverage_global: false,
          skip_rules_coverage_global: false,
          skip_trs_coverage_global: false,
          skip_contract_coverage_global: false,
          skip_ir_coverage_global: false,
          skip_or_coverage_global: false,
          skip_decisions_coverage_global: false,
          require_task_validation: true,
          min_confidence: 80,
          min_completeness: 80,
          max_drift: 20,
          require_spec_validation: true,
          min_spec_completeness: 80,
          min_spec_assertiveness: 80,
          max_spec_ambiguity: 30,
        }}
        canReadChecklist
        canExecuteChecklist
        onClose={() => {}}
        onSubmitted={() => {}}
      />,
    );

    expect(await screen.findByTestId('checklist-state-status')).toHaveTextContent(
      'not_started',
    );
    screen.getAllByPlaceholderText(/Why \d+\?/).forEach((textarea) => {
      fireEvent.change(textarea, {
        target: { value: 'A sufficiently detailed reason.' },
      });
    });
    fireEvent.change(
      screen.getByPlaceholderText(
        'Provide context for your overall judgment (min 20 chars)',
      ),
      { target: { value: 'A sufficiently detailed overall justification.' } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    const submitButton = screen.getByRole('button', {
      name: 'Submit Validation',
    });
    expect(submitButton).toBeDisabled();

    apiMock.getSpecChecklistState.mockResolvedValue(currentState);
    fireEvent.click(
      screen.getByRole('button', { name: 'Refresh checklist state' }),
    );
    await waitFor(() =>
      expect(screen.getByTestId('checklist-state-status')).toHaveTextContent(
        'current',
      ),
    );
    expect(submitButton).toBeEnabled();
  });
});
