import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { RequirementVerificationPanel } from '../RequirementVerificationPanel';
import { DeliveryEvidencePanel } from '@/components/code-traceability/DeliveryEvidencePanel';
import { CardResumePanel } from '@/components/code-traceability/CardResumePanel';
import native from './fixtures/native-verification-context.json';

// Exact payload emitted by the native relational + REST/MCP parity test.
// Backend uses typed reader identities and real authorization; network is mocked here.
const api = vi.hoisted(() => ({
  getRequirementVerification: vi.fn(), getDeliveryEvidence: vi.fn(),
  getCardDeliveryResume: vi.fn(), getBoard: vi.fn(), getSpec: vi.fn(),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
afterEach(cleanup);

it('renders inherited responsibility, promoted IR pending work and partial authenticated proof without global completion', async () => {
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
  await screen.findByText('Inherited from: fr');
  const rule = native.plan.items.find(row => row.requirement_type === 'business_rule')!;
  fireEvent.click(screen.getByRole('heading', { name: rule.title }).closest('button')!);
  expect(screen.getByText('Inherited from: fr')).toBeVisible();
  expect(screen.getByText('Planning does not require a passing run and does not grant delivery credit.')).toBeInTheDocument();
  for (const ir of native.promoted) {
    const toggle = screen.getByRole('heading', { name: ir.title }).closest('button')!;
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    const detail = document.getElementById(toggle.getAttribute('aria-controls')!)!;
    const reference = within(detail).getByText('Requirement reference').parentElement!;
    expect(within(reference).getByText(ir.id)).toBeVisible();
  }
  expect(await screen.findByText('Blocked')).toBeInTheDocument();
  expect(await screen.findByText(/Latest checkpoint by executor/)).toBeInTheDocument();
  expect(screen.getByText(/Progress recording is unavailable/)).toBeInTheDocument();
  expect(screen.getByText(/Workspace access and recovery are unknown/)).toBeInTheDocument();
  expect(screen.getAllByText('✓')).toHaveLength(6);
  const coverage = screen.getByTestId('delivery-coverage-table');
  const functional = within(coverage).getByText('Block after five failures', { selector: 'summary' });
  fireEvent.click(functional);
  const proof = within(functional.closest('details')!);
  expect(proof.getByText('Card: test-card · Scenario: ts')).toBeVisible();
  expect(proof.getByText('Result: passed · Current verified run')).toBeVisible();
  expect(proof.getByText('Implementation author: agent-1')).toBeVisible();
  expect(proof.getByText('Verification author: agent-1')).toBeVisible();
  expect(proof.getByText(/src\/file.py · source-main @ a{40} · execution/)).toBeVisible();
  for (const ir of native.promoted) {
    const summary = within(coverage).getByText(ir.title, { selector: 'summary' });
    const row = within(summary.closest('tr')!);
    expect(row.getByTitle('No accepted proof')).toHaveTextContent('◌');
    expect(row.getByTitle('Missing / stale')).toHaveTextContent('◌');
    fireEvent.click(summary);
    expect(row.getByText('No admitted implementation evidence.')).toBeVisible();
    expect(row.getByText('No admitted verification evidence.')).toBeVisible();
  }
  expect(api.getRequirementVerification).toHaveBeenCalledTimes(1);
  expect(api.getDeliveryEvidence).toHaveBeenCalledTimes(1);
  expect(api.getCardDeliveryResume).toHaveBeenCalledTimes(1);
  expect(api.getSpec).not.toHaveBeenCalled();
});
