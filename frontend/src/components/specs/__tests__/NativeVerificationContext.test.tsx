import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { RequirementVerificationPanel } from '../RequirementVerificationPanel';
import { DeliveryEvidencePanel } from '@/components/code-traceability/DeliveryEvidencePanel';
import { CardResumePanel } from '@/components/code-traceability/CardResumePanel';
import native from './fixtures/native-verification-context.json';

// Exact payload emitted by the native relational + REST/MCP parity test.
// Authentication is tested at the backend; here the network boundary is mocked.
const api = vi.hoisted(() => ({
  getRequirementVerification: vi.fn(), getDeliveryEvidence: vi.fn(),
  getCardDeliveryResume: vi.fn(), getBoard: vi.fn(), getSpec: vi.fn(),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
afterEach(cleanup);

it('renders inherited responsibility, promoted IR pending work and partial checkpoint without proof credit', async () => {
  api.getRequirementVerification.mockResolvedValue(native.plan);
  api.getDeliveryEvidence.mockResolvedValue(native.delivery);
  api.getCardDeliveryResume.mockResolvedValue(native.resume);
  api.getBoard.mockResolvedValue({ id: 'board', settings: { delivery_evidence_gate: 'blocking' } });
  render(<>
    <RequirementVerificationPanel
      scope={{ boardId: 'board', specId: 'spec', version: native.plan.spec_version, edition: native.plan.spec_edition }}
      canRead canReadPlanning canEdit={() => false} options={[]} criteria={[]} onSaved={async () => {}} />
    <DeliveryEvidencePanel boardId="board" specId="spec" />
    <CardResumePanel boardId="board" specId="spec" cardId="implementation-card" edition={native.resume.edition} />
  </>);
  fireEvent.click(screen.getByRole('button', { name: 'Review requirement qualification' }));
  fireEvent.click(screen.getByText('Read accumulated delivery context'));
  expect(await screen.findByText('Inherited from: fr')).toBeInTheDocument();
  expect(screen.getByText('Planning does not require a passing run and does not grant delivery credit.')).toBeInTheDocument();
  for (const ir of native.promoted) {
    expect(screen.getByText(new RegExp(ir.title + ' · ' + ir.id))).toBeInTheDocument();
  }
  expect(await screen.findByText('Blocked')).toBeInTheDocument();
  expect(await screen.findByText(/Latest checkpoint by executor/)).toBeInTheDocument();
  expect(screen.getByText(/Progress recording is unavailable/)).toBeInTheDocument();
  expect(screen.getByText(/Workspace access and recovery are unknown/)).toBeInTheDocument();
  expect(screen.queryByText('✓')).not.toBeInTheDocument();
  expect(api.getRequirementVerification).toHaveBeenCalledTimes(1);
  expect(api.getDeliveryEvidence).toHaveBeenCalledTimes(1);
  expect(api.getCardDeliveryResume).toHaveBeenCalledTimes(1);
  expect(api.getSpec).not.toHaveBeenCalled();
});
