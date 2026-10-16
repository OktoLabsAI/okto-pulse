import { useState } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { DeliveryReportBatchEditor } from '../DeliveryReportBatchEditor';
import type { CardDeliveryBatchDraft, DeliverySelectionInput } from '@/types/delivery-evidence';

const api = vi.hoisted(() => ({ getDeliveryEvidence: vi.fn(), getBoard: vi.fn(), recordCardDeliveryEvidence: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const selection: DeliverySelectionInput = { expected_card_version: 3, expected_spec_edition: 2, expected_delivery_revision: 0, record_ids: [] };
const draft: CardDeliveryBatchDraft = { contract_version: 'card-delivery-batch/v1', expected_card_version: 3, expected_spec_edition: 2, expected_delivery_revision: 0, entries: [] };
beforeEach(() => {
  vi.clearAllMocks();
  api.getBoard.mockResolvedValue({ settings: {} });
  api.getDeliveryEvidence.mockResolvedValue({ edition: 2, rows: [], candidates: [], implementations: [], rejected_record_ids: [],
    per_card: [{ card_id: 'card', card_version: 3, delivery_revision: 0, status: 'in_progress', obligations: [], satisfied: false }] });
});
afterEach(cleanup);

function Harness({ initial, basis = selection, canWrite = true }: { initial?: CardDeliveryBatchDraft; basis?: DeliverySelectionInput; canWrite?: boolean }) {
  const [value, setValue] = useState(initial);
  return <DeliveryReportBatchEditor boardId="board" card={{ id: 'card', card_type: 'normal', spec_id: 'spec' }}
    selection={basis} draft={value} onChange={setValue} canProgress={canWrite} canRecord={canWrite} canTest={false} />;
}
async function stage(summary = 'Context discovered') {
  fireEvent.change(await screen.findByLabelText('Work recorded'), { target: { value: summary } });
  fireEvent.change(screen.getByLabelText('Remaining work'), { target: { value: 'Review this finding' } });
  fireEvent.change(screen.getByLabelText('Code change in this checkpoint'), { target: { value: 'none' } });
  fireEvent.click(screen.getByRole('button', { name: 'Add progress to report draft' }));
}

it('keeps multiple entries as removable drafts with no network writes', async () => {
  render(<Harness />); await stage(); await stage('Second finding');
  const entries = screen.getByRole('list', { name: 'Unsent delivery entries' });
  await waitFor(() => expect(within(entries).getAllByRole('listitem')).toHaveLength(2));
  fireEvent.click(screen.getByRole('button', { name: 'Remove draft entry 1' }));
  expect(entries).not.toHaveTextContent('Context discovered'); expect(entries).toHaveTextContent('Second finding');
  expect(api.recordCardDeliveryEvidence).not.toHaveBeenCalled();
});

it.each(['expected_card_version', 'expected_spec_edition', 'expected_delivery_revision'] as const)('refuses a stale %s and preserves the typed entry', async field => {
  render(<Harness basis={{ ...selection, [field]: selection[field] + 1 }} />); await stage();
  expect(await screen.findByRole('alert')).toHaveTextContent('evidence changed');
  expect(screen.getByLabelText('Work recorded')).toHaveValue('Context discovered');
  expect(screen.getByRole('list', { name: 'Unsent delivery entries' })).toBeEmptyDOMElement();
  expect(api.recordCardDeliveryEvidence).not.toHaveBeenCalled();
});

it.each(['batch', 'selection'] as const)('enforces the aggregate %s limit before clearing the form', async limit => {
  const initial = limit === 'batch' ? { ...draft, entries: Array.from({ length: 50 }, (_, i) => ({ client_ref: `p${i}`, kind: 'progress' as const, obligation_refs: [], justification: 'Existing draft' })) } : undefined;
  render(<Harness initial={initial} basis={limit === 'selection' ? { ...selection, record_ids: Array.from({ length: 200 }, (_, i) => `r${i}`) } : selection} />);
  await stage();
  expect(await screen.findByRole('alert')).toHaveTextContent('up to 50 new entries');
  expect(screen.getByLabelText('Work recorded')).toHaveValue('Context discovered');
  expect(api.recordCardDeliveryEvidence).not.toHaveBeenCalled();
});

it('does not offer proof or progress drafts without the corresponding permissions', async () => {
  render(<Harness canWrite={false} />);
  await screen.findByText(/Recording progress requires/);
  expect(screen.queryByRole('button', { name: 'Add progress to report draft' })).not.toBeInTheDocument();
  expect(screen.queryByTestId('dod-record-button')).not.toBeInTheDocument();
  expect(api.recordCardDeliveryEvidence).not.toHaveBeenCalled();
});
