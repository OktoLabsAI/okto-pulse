import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { CardDeliveryDoDPanel } from '../CardDeliveryDoDPanel';
import type { DeliveryEvidenceProjection } from '@/types/delivery-evidence';

const api = vi.hoisted(() => ({ getDeliveryEvidence: vi.fn(), getBoard: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
afterEach(cleanup);

it('U05: pre-Done accepted proof agrees across the row, counter and blocking notice', async () => {
  const value: DeliveryEvidenceProjection = {
    board_id: 'b', spec_id: 's', edition: 1, version: 1, status: 'in_progress',
    allowed: false, complete: true, blockers: ['delivery_implementation_missing'],
    rejected_record_ids: [], rows: [], tests: [], candidates: [], records: [],
    implementations: [{ id: 'proof', card_id: 'task', explanation: 'Observed committed implementation',
      bindings: [{ obligation_ref: 'fr:ports', semantic_sha256: 'a'.repeat(64) }],
      contributions: [{ binding: { obligation_ref: 'fr:ports', semantic_sha256: 'a'.repeat(64) },
        contribution: 'complete', execution_ids: ['execution'] }],
      admitted_obligation_refs: ['fr:ports'], ready_obligation_refs: ['fr:ports'],
      executions: [{ execution_id: 'execution', target_id: 'target', source_ref: 'repo',
        relative_path: 'src/ports.py', result_revision: 'a'.repeat(40), current_accepted_execution: true }] }],
    per_card: [{ card_id: 'task', title: 'Implement public ports', card_type: 'normal',
      status: 'in_progress', satisfied: false,
      obligations: [{ ref: 'fr:ports', title: 'Use public ports', implementation_satisfied: false }] }],
  };
  api.getDeliveryEvidence.mockResolvedValue(value);
  api.getBoard.mockResolvedValue({ id: 'b', settings: { delivery_evidence_gate: 'blocking' } });
  render(<CardDeliveryDoDPanel boardId="b" card={{ id: 'task', card_type: 'normal', spec_id: 's' }} />);
  expect(await screen.findByText('✓ Implementation')).toBeInTheDocument();
  expect(screen.getByTestId('dod-gate-pill')).toHaveTextContent('0 of 1 unproven');
  expect(screen.queryByTestId('dod-blocked-banner')).not.toBeInTheDocument();
});
