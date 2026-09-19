import { act, fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import type { IntegrationRequirement } from '@/types';
import type { ArchitectureClassificationBatch, ArchitectureClassificationReceipt, ArchitectureClassificationReviewItem } from '@/types/architecture-classifications';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { ArchitectureClassificationAuthoring } from '../ArchitectureClassificationAuthoring';

const api = vi.hoisted(() => ({ classifyArchitectureCandidates: vi.fn(), getArchitectureClassifications: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const onApplied = vi.fn();
function item(id = 'a'): ArchitectureClassificationReviewItem {
  return { candidate_id: id, name: `Contract ${id}`, root_design_id: `root-${id}`, interface_id: 'boundary', state: 'pending',
    current_source_digest: id.repeat(64), analyzed_source_digest: null, source_variant_count: 1, source_digests: [id.repeat(64)], source_digests_truncated: false,
    decision_count: 0, dispositions: [], issues: [], remainder_state: null };
}
function ir(id = 'ir_existing', status: IntegrationRequirement['status'] = 'active'): IntegrationRequirement {
  return { id, title: `Existing ${id}`, integration_type: 'event', status, description: 'Existing obligation', provider: null, consumer: null, endpoint: null, method: null, contract_ref: null, data_contract: null, notes: null, linked_requirements: null, linked_api_contracts: null, linked_task_ids: null };
}
const props = { boardId: 'board', specId: 'spec', specVersion: 9, specEdition: 2, sourceReady: true, canClassify: true, canPromote: true, canAssociate: true, requirements: [ir()], onApplied,
  items: [item()], renderItem: (row: ArchitectureClassificationReviewItem, selection: ReactNode) => <article key={row.candidate_id}>{selection}<span>{row.name}</span></article> };
function receipt(batch: ArchitectureClassificationBatch, changes: Partial<ArchitectureClassificationReceipt> = {}): ArchitectureClassificationReceipt {
  return { contract_version: 'architecture-classification/v1', board_id: 'board', spec_id: 'spec', spec_edition: 2, spec_version: 10, idempotency_key: batch.idempotency_key, created_ir_ids: [], decisions: [], pending_checks: ['requirement_readiness_and_spec_start_gates_not_evaluated'], replayed: false, ...changes };
}
function suggestion(changes: Record<string, unknown> = {}) {
  return { board_id: 'board', spec_id: 'spec', spec_version: 9, spec_edition: 2, profile: 'detail', items: [{ ...item(), promotion_suggestion: { scope_paths: [''], requires_author_review: true, missing_required_fields: [], proposed_ir: {
    title: 'Publish orders', integration_type: 'event', data_contract: { event_schema: {}, error_contract: { code: 'BUSY' }, participants: ['first', 'second'] }, contract_ref: 'https://do-not-fetch.invalid/v3',
  } } }], ...changes };
}
function select(id = 'a') { fireEvent.click(screen.getByRole('checkbox', { name: `Select Contract ${id}` })); }
function choose(value: string) { fireEvent.change(screen.getByRole('combobox', { name: 'Decision' }), { target: { value } }); }
function change(name: string, value: string) { fireEvent.change(screen.getByLabelText(name), { target: { value } }); }
function queueContext() { choose('context_only'); change('Context reason', 'Outside this delivery'); fireEvent.click(screen.getByRole('button', { name: 'Queue decision' })); }
function save() { fireEvent.click(screen.getByRole('button', { name: 'Save queued classifications' })); }
beforeEach(() => {
  api.classifyArchitectureCandidates.mockReset().mockImplementation(async (_board, _spec, batch) => receipt(batch));
  api.getArchitectureClassifications.mockReset(); onApplied.mockReset().mockResolvedValue(undefined);
});

it('retains explicit selections across pages and saves only those candidates once', async () => {
  const { rerender } = render(<ArchitectureClassificationAuthoring {...props} />);
  select();
  rerender(<ArchitectureClassificationAuthoring {...props} items={[]} sourceReady={false} />);
  expect(screen.getByText('1 selected across pages · 0 queued decisions')).toBeInTheDocument();
  rerender(<ArchitectureClassificationAuthoring {...props} items={[item('b'), item('c')]} />);
  select('b'); queueContext(); save();
  await screen.findByText(/Classifications saved/);
  expect(api.classifyArchitectureCandidates).toHaveBeenCalledTimes(1);
  const [board, spec, batch] = api.classifyArchitectureCandidates.mock.calls[0];
  expect([board, spec, batch.expected_spec_version, batch.expected_spec_edition]).toEqual(['board', 'spec', 9, 2]);
  expect(batch.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
  expect(batch.decisions).toEqual(['a', 'b'].map(candidate_ref => ({ candidate_ref, expected_source_digest: candidate_ref.repeat(64), disposition: 'context_only', scope_paths: [''], reason: 'Outside this delivery' })));
  expect(onApplied).toHaveBeenCalledTimes(1);
});

it('authors multiple IRs for a partial contract without defaulting HTTP or adopting the remainder', async () => {
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('promote_to_ir');
  expect(screen.getByLabelText('Integration type')).toHaveValue('');
  fireEvent.click(screen.getByLabelText('Adopt only selected contract parts'));
  change('Named contract paths (JSON Pointer, one per line)', '/event_schema/publish');
  change('Remaining context reason', 'The consumer is outside this Spec');
  change('IR title', 'Publish event'); change('Integration type', 'event'); change('Data contract (JSON object)', '{}');
  fireEvent.click(screen.getByRole('button', { name: 'Add another proposed IR' }));
  const second = within(screen.getByRole('group', { name: 'Proposed IR 2' }));
  fireEvent.change(second.getByLabelText('IR title'), { target: { value: 'Export file' } });
  fireEvent.change(second.getByLabelText('Integration type'), { target: { value: 'file' } });
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' })); save();
  await screen.findByText(/Classifications saved/);
  expect(api.classifyArchitectureCandidates.mock.calls[0][2].decisions[0]).toEqual({ candidate_ref: 'a', expected_source_digest: 'a'.repeat(64), disposition: 'promote_to_ir', scope_paths: ['/event_schema/publish'], remainder_reason: 'The consumer is outside this Spec',
    integration_requirements: [{ title: 'Publish event', integration_type: 'event', data_contract: {} }, { title: 'Export file', integration_type: 'file' }] });
});

it('offers source-backed fields only after explicit acceptance and keeps provider/consumer/method blank', async () => {
  api.getArchitectureClassifications.mockResolvedValue(suggestion());
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('promote_to_ir');
  expect(api.getArchitectureClassifications).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Replace first IR with source suggestion' }));
  await screen.findByText(/Source suggestion loaded/);
  expect(screen.getByLabelText('IR title')).toHaveValue('Publish orders');
  expect(screen.getByLabelText('Integration type')).toHaveValue('event');
  for (const field of ['Provider', 'Consumer', 'Method (only if applicable)']) expect(screen.getByLabelText(field)).toHaveValue('');
  expect(screen.getByLabelText('Data contract (JSON object)')).toHaveValue(JSON.stringify(suggestion().items[0].promotion_suggestion.proposed_ir.data_contract, null, 2));
  expect(api.classifyArchitectureCandidates).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' })); save();
  await screen.findByText(/Classifications saved/);
  expect(api.classifyArchitectureCandidates.mock.calls[0][2].decisions[0].integration_requirements[0]).toEqual(suggestion().items[0].promotion_suggestion.proposed_ir);
});

it('clears an untouched whole-source proposal when selecting partial adoption', async () => {
  api.getArchitectureClassifications.mockResolvedValue(suggestion());
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('promote_to_ir');
  fireEvent.click(screen.getByRole('button', { name: 'Replace first IR with source suggestion' }));
  await screen.findByText(/Source suggestion loaded/);
  fireEvent.click(screen.getByLabelText('Adopt only selected contract parts'));
  expect(screen.getByLabelText('Data contract (JSON object)')).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Replace first IR with source suggestion' })).toBeDisabled();
  expect(screen.getByText(/whole-contract proposal was cleared/)).toBeInTheDocument();
});

it('associates several candidates to the same active local IR without writing or delivering it', async () => {
  render(<ArchitectureClassificationAuthoring {...props} items={[item(), item('b')]} requirements={[ir(), ir('revoked', 'revoked'), ir('duplicate'), ir('duplicate')]} />);
  select(); select('b'); choose('associate_existing_ir');
  expect(screen.queryByText(/Existing revoked/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Existing duplicate/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('Existing ir_existing (ir_existing)'));
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' })); save();
  await screen.findByText(/Classifications saved/);
  const decisions = api.classifyArchitectureCandidates.mock.calls[0][2].decisions;
  expect(decisions).toHaveLength(2);
  for (const decision of decisions) { expect(decision.integration_requirement_refs).toEqual(['ir_existing']); expect(decision.integration_requirements).toBeUndefined(); expect(decision.disposition).toBe('associate_existing_ir'); }
});

it.each(['not JSON', '[]', 'null', '{"threshold":1e999}'])('rejects invalid or non-finite contract %s without silently replacing it', value => {
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('promote_to_ir');
  change('IR title', 'Orders'); change('Integration type', 'event'); change('Data contract (JSON object)', value);
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' }));
  expect(screen.getByRole('alert')).toHaveTextContent('Data contract must be a valid JSON object.');
  expect(screen.getByRole('button', { name: 'Save queued classifications' })).toBeDisabled();
  expect(api.classifyArchitectureCandidates).not.toHaveBeenCalled();
});

it('requires a context reason and explicit promotion type, with no implicit pending classification', () => {
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('context_only');
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' }));
  expect(screen.getByRole('alert')).toHaveTextContent('Explain why this scope is context only.');
  choose('promote_to_ir'); change('IR title', 'Unspecified type');
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' }));
  expect(screen.getByRole('alert')).toHaveTextContent('explicit integration type');
  expect(api.classifyArchitectureCandidates).not.toHaveBeenCalled();
});

it('keeps context available without IR writer permissions and disables the other choices', async () => {
  render(<ArchitectureClassificationAuthoring {...props} canPromote={false} canAssociate={false} />); select();
  expect(screen.getByRole('option', { name: 'Promote to IR' })).toBeDisabled();
  expect(screen.getByRole('option', { name: 'Associate existing IR' })).toBeDisabled();
  queueContext(); save(); await screen.findByText(/Classifications saved/);
});

it('retries the identical uncertain submission and never duplicates the key, payload or reload', async () => {
  api.classifyArchitectureCandidates.mockRejectedValueOnce(new Error('SECRET timeout')).mockImplementationOnce(async (_board, _spec, batch) => receipt(batch, { replayed: true }));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext(); save();
  await screen.findByText(/submission outcome is unknown/);
  expect(screen.getByRole('button', { name: 'Remove queued decision 1' })).toBeDisabled();
  expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry exact submission' }));
  await screen.findByText(/original classification was confirmed/);
  expect(api.classifyArchitectureCandidates.mock.calls[1]).toEqual(api.classifyArchitectureCandidates.mock.calls[0]);
  expect(onApplied).toHaveBeenCalledTimes(1);
});

it('does not treat a later permission error as proof that an uncertain earlier write never committed', async () => {
  api.classifyArchitectureCandidates.mockRejectedValueOnce(new Error('network')).mockRejectedValueOnce(new AuthenticatedFetchError({ status: 403, message: 'private' }));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext(); save();
  fireEvent.click(await screen.findByRole('button', { name: 'Retry exact submission' }));
  await screen.findByRole('button', { name: 'Retry exact submission' });
  expect(screen.queryByRole('button', { name: 'Discard rejected batch' })).not.toBeInTheDocument();
  expect(onApplied).not.toHaveBeenCalled();
});

it.each([403, 404, 409, 413, 422])('shows a safe %i refusal with no partial success, auto retry or retargeting', async status => {
  api.classifyArchitectureCandidates.mockRejectedValue(new AuthenticatedFetchError({ status, message: 'SECRET schema' }));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext(); save();
  await screen.findByRole('button', { name: 'Discard rejected batch' });
  expect(api.classifyArchitectureCandidates).toHaveBeenCalledTimes(1);
  expect(onApplied).not.toHaveBeenCalled();
  expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
  if (status === 409) expect(screen.getByRole('alert')).toHaveTextContent('Refresh and review');
  fireEvent.click(screen.getByRole('button', { name: 'Discard rejected batch' }));
  expect(screen.getByRole('button', { name: 'Save queued classifications' })).toBeDisabled();
});

it('ignores a late write result after the authoring scope is unmounted', async () => {
  let complete!: (value: ArchitectureClassificationReceipt) => void;
  api.classifyArchitectureCandidates.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  const { unmount } = render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext(); save();
  expect(api.classifyArchitectureCandidates).toHaveBeenCalledTimes(1);
  const batch = api.classifyArchitectureCandidates.mock.calls[0][2];
  unmount(); await act(async () => { complete(receipt(batch)); });
  expect(onApplied).not.toHaveBeenCalled();
});

it('prevents duplicate clicks during one in-flight submission', async () => {
  let complete!: (value: ArchitectureClassificationReceipt) => void;
  api.classifyArchitectureCandidates.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext();
  const button = screen.getByRole('button', { name: 'Save queued classifications' });
  fireEvent.click(button); fireEvent.click(button);
  expect(api.classifyArchitectureCandidates).toHaveBeenCalledTimes(1);
  await act(async () => { complete(receipt(api.classifyArchitectureCandidates.mock.calls[0][2])); });
});

it('does not resubmit a successful classification if the following Spec reload fails', async () => {
  onApplied.mockRejectedValueOnce(new Error('reload failed')).mockResolvedValueOnce(undefined);
  render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext(); save();
  await screen.findByText(/Classifications were saved. Refresh/);
  expect(screen.queryByRole('button', { name: 'Retry exact submission' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Reload Spec after saving' }));
  await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(2));
  expect(api.classifyArchitectureCandidates).toHaveBeenCalledTimes(1);
});

it('rejects an unconfirmed receipt and retains the original attempt for replay', async () => {
  api.classifyArchitectureCandidates.mockImplementation(async (_board, _spec, batch) => receipt(batch, { spec_id: 'foreign' }));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext(); save();
  await screen.findByRole('button', { name: 'Retry exact submission' });
  expect(onApplied).not.toHaveBeenCalled();
});

it('cancels a suggestion when authored fields change and ignores its late result', async () => {
  let complete!: (value: ReturnType<typeof suggestion>) => void;
  api.getArchitectureClassifications.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('promote_to_ir');
  fireEvent.click(screen.getByRole('button', { name: 'Replace first IR with source suggestion' }));
  const signal = api.getArchitectureClassifications.mock.calls[0][2] as AbortSignal;
  change('IR title', 'My authored title');
  expect(signal.aborted).toBe(true);
  await act(async () => { complete(suggestion()); });
  expect(screen.getByLabelText('IR title')).toHaveValue('My authored title');
});

it.each([{ spec_version: 10 }, { spec_edition: 3 }, { spec_id: 'foreign' }])('refuses a suggestion from a changed scope %j', async mismatch => {
  api.getArchitectureClassifications.mockResolvedValue(suggestion(mismatch));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('promote_to_ir');
  fireEvent.click(screen.getByRole('button', { name: 'Replace first IR with source suggestion' }));
  await screen.findByText(/source suggestion is unavailable or changed/);
  expect(screen.getByLabelText('IR title')).toHaveValue('');
});

it('checks aggregate UTF-8 size before sending and keeps the draft editable', () => {
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('context_only');
  change('Context reason', 'é'.repeat(132000));
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' })); save();
  expect(screen.getByRole('alert')).toHaveTextContent('exceeds 256 KiB');
  expect(api.classifyArchitectureCandidates).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Remove queued decision 1' })).not.toBeDisabled();
});

it('disables new submissions while sources or the Spec snapshot are unavailable', () => {
  const { rerender } = render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext();
  rerender(<ArchitectureClassificationAuthoring {...props} sourceReady={false} />);
  expect(screen.getByRole('button', { name: 'Save queued classifications' })).toBeDisabled();
  expect(screen.getByRole('checkbox', { name: 'Select Contract a' })).toBeDisabled();
  expect(api.classifyArchitectureCandidates).not.toHaveBeenCalled();
});

it('submits promotion, association and context together through a single atomic batch', async () => {
  render(<ArchitectureClassificationAuthoring {...props} items={[item(), item('b'), item('c')]} />);
  select(); queueContext();
  select('b'); choose('associate_existing_ir'); fireEvent.click(screen.getByLabelText('Existing ir_existing (ir_existing)'));
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' }));
  select('c'); choose('promote_to_ir'); change('IR title', 'Publish new event'); change('Integration type', 'event');
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' }));
  expect(api.classifyArchitectureCandidates).not.toHaveBeenCalled(); save();
  await screen.findByText(/Classifications saved/);
  expect(api.classifyArchitectureCandidates).toHaveBeenCalledTimes(1);
  expect(api.classifyArchitectureCandidates.mock.calls[0][2].decisions.map((decision: { disposition: string }) => decision.disposition)).toEqual(['context_only', 'associate_existing_ir', 'promote_to_ir']);
});

it('preserves a definitely rejected batch for correction and uses a new key only for the edited intent', async () => {
  api.classifyArchitectureCandidates.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 422, message: 'Invalid linked reference' }))
    .mockImplementationOnce(async (_board, _spec, batch) => receipt(batch));
  render(<ArchitectureClassificationAuthoring {...props} />); select(); queueContext(); save();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit rejected batch' }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit queued decision 1' }));
  expect(screen.getByLabelText('Context reason')).toHaveValue('Outside this delivery');
  expect(screen.getByRole('button', { name: 'Save queued classifications' })).toBeDisabled();
  change('Context reason', 'Corrected authored reason');
  fireEvent.click(screen.getByRole('button', { name: 'Update queued decision' })); save();
  await screen.findByText(/Classifications saved/);
  const first = api.classifyArchitectureCandidates.mock.calls[0][2];
  const second = api.classifyArchitectureCandidates.mock.calls[1][2];
  expect(second.idempotency_key).not.toBe(first.idempotency_key);
  expect(second.decisions).toEqual([{ ...first.decisions[0], reason: 'Corrected authored reason' }]);
  expect(first.decisions[0].reason).toBe('Outside this delivery');
});

it('does not allow unresolved, retired or unavailable candidates into an authored batch', () => {
  const states = ['unresolved', 'retired', 'unavailable'] as const;
  render(<ArchitectureClassificationAuthoring {...props} items={states.map((state, index) => ({ ...item(String(index)), state }))} />);
  for (let index = 0; index < states.length; index++) expect(screen.getByRole('checkbox', { name: `Select Contract ${index}` })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save queued classifications' })).toBeDisabled();
});

it('preserves spaces in named JSON Pointer scopes instead of selecting a different member', async () => {
  render(<ArchitectureClassificationAuthoring {...props} />); select(); choose('context_only');
  fireEvent.click(screen.getByLabelText('Adopt only selected contract parts'));
  change('Named contract paths (JSON Pointer, one per line)', '/event_schema/a \n/event_schema/ b');
  change('Remaining context reason', 'Other members remain context'); change('Context reason', 'These parts are contextual');
  fireEvent.click(screen.getByRole('button', { name: 'Queue decision' })); save();
  await screen.findByText(/Classifications saved/);
  expect(api.classifyArchitectureCandidates.mock.calls[0][2].decisions[0].scope_paths).toEqual(['/event_schema/a ', '/event_schema/ b']);
});
