import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Spec } from '@/types';
import { RulesTab } from '../RulesTab';
import { DecisionsTab } from '../DecisionsTab';
import { IntegrationRequirementsTab } from '../IntegrationRequirementsTab';
import { ObservabilityRequirementsTab } from '../ObservabilityRequirementsTab';
import { TechnicalRequirementsTab } from '../TechnicalRequirementsTab';
import { ContractsTab } from '../ContractsTab';

vi.mock('@/hooks/useCognitivePendingBadges', () => ({ useCognitivePendingBadges: () => ({ badges: {} }) }));
const api = vi.hoisted(() => ({ getDecisionReviews: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
beforeEach(() => api.getDecisionReviews.mockResolvedValue({ board_id: 'board', spec_id: 'spec', decisions: [], complete: true }));

const currentSpec = (extra: Record<string, unknown> = {}): Spec => ({
  id: 'spec', board_id: 'board', version: 8, edition: 1, functional_requirements: [
    { id: 'fr-stable', text: 'Return orders' },
  ], business_rules: [], decisions: [], integration_requirements: [],
  observability_requirements: [], technical_requirements: [], ...extra,
} as unknown as Spec);

const families = [
  { Component: RulesTab, open: 'Add Business Rule', submit: 'Add Rule', fields: [
    ['Rule title', 'Rule'], ['Rule description — what must be enforced', 'Return all orders'],
    ['When: condition...', 'Requested'], ['Then: action/result...', 'Return orders'],
  ] },
  { Component: DecisionsTab, open: 'Add Decision', submit: 'Add Decision', fields: [
    ["Decision title — e.g. 'Use embedded graph storage over an external graph database'", 'Decision'],
    ['Rationale — why this choice was made', 'Orders are required'],
  ] },
  { Component: IntegrationRequirementsTab, open: 'Add Integration Requirement', submit: 'Add IR', fields: [
    ['Integration requirement title', 'Integration'],
    ['Contract, queue, API, stored procedure, event, or file integration expectation', 'Publish orders'],
  ] },
  { Component: ObservabilityRequirementsTab, open: 'Add Observability Requirement', submit: 'Add OR', fields: [
    ['Observability requirement title', 'Observability'],
    ['Dashboard, metric, alert, log, trace, or SLO expectation', 'Count orders'],
  ] },
];

const draftFamilies = [...families,
  { Component: ContractsTab, open: 'Add API Contract', submit: 'Add Contract', fields: [
    ['/api/v1/resource or component name', '/orders'],
    ['Description of this endpoint/interface', 'Read orders'],
  ] },
  { Component: TechnicalRequirementsTab, open: 'Add Technical Requirement', submit: 'Add', fields: [
    ['Add a technical constraint...', 'No concrete adapters in the domain'],
  ] },
];

describe('structured collection draft confirmation', () => {
  it.each(families)('$open requires an explicit correction when a selected FR is removed', ({ Component, open, submit, fields }) => {
    const onUpdate = vi.fn();
    const view = render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: /FR\d+: Return orders/ }));
    view.rerender(<Component spec={currentSpec({ version: 9, functional_requirements: [] })} onUpdate={onUpdate} />);
    expect(screen.getByRole('alert')).toHaveTextContent('fr-stable');
    fireEvent.click(screen.getByRole('button', { name: 'Review current base' }));
    expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    expect(screen.getByDisplayValue(fields[0][1])).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove unavailable references' }));
    expect(screen.getByRole('button', { name: submit })).toBeEnabled();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it.each([
    { Component: RulesTab, field: 'business_rules', item: { id: 'item', title: 'Saved title', rule: 'Normative rule', when: 'Condition', then: 'Result' }, input: 'Saved title' },
    { Component: ContractsTab, field: 'api_contracts', item: { id: 'item', contract_type: 'http', method: 'GET', path: '/saved', description: 'Saved contract' }, input: 'Saved contract' },
    { Component: TechnicalRequirementsTab, field: 'technical_requirements', item: { id: 'item', text: 'Saved constraint' }, input: 'Saved constraint' },
    { Component: IntegrationRequirementsTab, field: 'integration_requirements', item: { id: 'item', title: 'Saved title', description: 'Saved integration', integration_type: 'api', status: 'active' }, input: 'Saved title' },
    { Component: ObservabilityRequirementsTab, field: 'observability_requirements', item: { id: 'item', title: 'Saved title', description: 'Saved monitoring', signal_type: 'metric', status: 'active' }, input: 'Saved title' },
    { Component: DecisionsTab, field: 'decisions', item: { id: 'item', title: 'Saved title', rationale: 'Saved rationale', status: 'active' }, input: 'Saved title' },
  ])('$field preserves a removed item draft without recreating it', async ({ Component, field, item, input }) => {
    const onUpdate = vi.fn();
    const view = render(<Component spec={currentSpec({ [field]: [item] })} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByTitle('Edit'));
    fireEvent.change(screen.getByDisplayValue(input), { target: { value: 'Unsaved local content' } });
    view.rerender(<Component spec={currentSpec({ version: 9, [field]: [] })} onUpdate={onUpdate} />);
    expect(screen.getByDisplayValue('Unsaved local content')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('no longer available');
    expect(onUpdate).not.toHaveBeenCalled();
    await act(async () => {});
  });

  it.each(draftFamilies)('$open preserves a refused draft and allows a corrected retry', async ({ Component, open, submit, fields }) => {
    const onUpdate = vi.fn().mockRejectedValueOnce(Object.assign(new Error('Business constraint refused'), { status: 409, code: 'business_constraint' })).mockResolvedValue(undefined);
    render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: submit }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Business constraint refused');
    expect(screen.getByDisplayValue(fields[0][1])).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review current base' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: submit }));
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(2));
    expect(onUpdate.mock.calls[1][1]).toBe(8);
  });

  it.each(draftFamilies)('$open keeps a native conflict blocked until a newer read is reviewed', async ({ Component, open, submit, fields }) => {
    const onUpdate = vi.fn().mockRejectedValueOnce(Object.assign(new Error('Version conflict'), { details: { error_code: 'version_conflict' } })).mockResolvedValue(undefined);
    const view = render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: submit }));
    expect(await screen.findByRole('button', { name: 'Review current base' })).toBeDisabled();
    expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    view.rerender(<Component spec={currentSpec({ version: 9 })} onUpdate={onUpdate} readCurrent={false} />);
    expect(screen.getByRole('button', { name: 'Review current base' })).toBeDisabled();
    view.rerender(<Component spec={currentSpec({ version: 9 })} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Review current base' }));
    fireEvent.click(screen.getByRole('button', { name: submit }));
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(2));
    expect(onUpdate.mock.calls[1][1]).toBe(9);
  });

  it.each(draftFamilies)('$open preserves fields through unavailable reads without permitting a save', async ({ Component, open, submit, fields }) => {
    const onUpdate = vi.fn();
    const view = render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    view.rerender(<Component spec={currentSpec()} onUpdate={onUpdate} readCurrent={false} />);
    expect(screen.getByDisplayValue(fields[0][1])).toBeInTheDocument();
    expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    view.rerender(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: submit }));
    await waitFor(() => expect(onUpdate).toHaveBeenCalledOnce());
  });

  it.each(draftFamilies)('$open never resubmits a partially confirmed collection sequence', async ({ Component, open, submit, fields }) => {
    const onUpdate = vi.fn().mockRejectedValue(Object.assign(new Error('Some changes were saved. Reopen after refresh.'), { code: 'partial_collection_write' }));
    const view = render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: submit }));
    await screen.findByRole('alert');
    view.rerender(<Component spec={currentSpec({ version: 9 })} onUpdate={onUpdate} />);
    expect(screen.getByDisplayValue(fields[0][1])).toBeInTheDocument();
    expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Review current base' })).not.toBeInTheDocument();
    expect(onUpdate).toHaveBeenCalledOnce();
  });

  it.each(draftFamilies)('$open discards protected fields when permission is lost or the Spec changes', ({ Component, open, fields }) => {
    const onUpdate = vi.fn();
    const view = render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    view.rerender(<Component spec={currentSpec()} onUpdate={onUpdate} canDelete={false} />);
    expect(screen.getByDisplayValue(fields[0][1])).toBeInTheDocument();
    view.rerender(<Component spec={currentSpec()} onUpdate={onUpdate} canCreate={false} canEdit={false} />);
    expect(screen.queryByDisplayValue(fields[0][1])).not.toBeInTheDocument();
    view.rerender(<Component spec={currentSpec({ id: 'other' })} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    expect(screen.getByPlaceholderText(fields[0][0])).toHaveValue('');
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it.each(draftFamilies)('$open keeps the draft until its write is confirmed', async ({ Component, open, submit, fields }) => {
    let finish!: () => void;
    const onUpdate = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: submit }));
    expect(onUpdate).toHaveBeenCalledOnce();
    expect(screen.getByDisplayValue(fields[0][1])).toBeInTheDocument();
    expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    await act(async () => finish());
    await waitFor(() => expect(screen.queryByDisplayValue(fields[0][1])).not.toBeInTheDocument());
  });

  it.each(draftFamilies)('$open requires explicit review of an externally changed base', async ({ Component, open, submit, fields }) => {
    const onUpdate = vi.fn();
    const view = render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    view.rerender(<Component spec={currentSpec({ version: 9 })} onUpdate={onUpdate} />);
    expect(screen.getByDisplayValue(fields[0][1])).toBeInTheDocument();
    expect(screen.getByRole('button', { name: submit })).toBeDisabled();
    expect(onUpdate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review current base' }));
    fireEvent.click(screen.getByRole('button', { name: submit }));
    await waitFor(() => expect(onUpdate).toHaveBeenCalledOnce());
    expect(onUpdate.mock.calls[0][1]).toBe(9);
  });
});

describe('current requirement identity in authoring tabs', () => {
  it.each(families)('$open writes the stable FR ID', ({ Component, open, submit, fields }) => {
    const onUpdate = vi.fn();
    render(<Component spec={currentSpec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: open }));
    for (const [placeholder, value] of fields) {
      fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
    }
    fireEvent.click(screen.getByRole('button', { name: /FR\d+: Return orders/ }));
    fireEvent.click(screen.getByRole('button', { name: submit }));
    expect(onUpdate).toHaveBeenCalledOnce();
    expect(onUpdate.mock.calls[0][0][0].linked_requirements).toEqual(['fr-stable']);
  });

  it.each(['0', 'Return orders', 'Return'])('does not credit rule reference %s as coverage', (reference) => {
    const rule = { id: 'br', title: 'Rule', rule: 'Return orders', when: 'Read', then: 'Return', linked_requirements: [reference] };
    const view = render(<RulesTab spec={currentSpec({ business_rules: [rule] })} onUpdate={vi.fn()} />);
    expect(screen.getByText('FR Coverage (0/1)')).toBeInTheDocument();
    view.rerender(<RulesTab spec={currentSpec({ business_rules: [{ ...rule, linked_requirements: ['fr-stable'] }] })} onUpdate={vi.fn()} />);
    expect(screen.getByText('FR Coverage (1/1)')).toBeInTheDocument();
  });

  it('edits a TR without generating identity or losing links and inactive history', () => {
    const onUpdate = vi.fn();
    const current = { id: 'tr-stable', text: 'Existing constraint', linked_task_ids: ['task'] };
    const previous = { id: 'tr-previous', text: 'Previous constraint', status: 'superseded' };
    render(<TechnicalRequirementsTab spec={currentSpec({ technical_requirements: [current, previous] })} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByTitle('Edit'));
    fireEvent.change(screen.getByDisplayValue('Existing constraint'), { target: { value: 'Updated constraint' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onUpdate.mock.calls[0][0]).toEqual([{ ...current, text: 'Updated constraint' }, previous]);
  });

  it('refuses a string TR without assigning an index identity', () => {
    const silence = vi.spyOn(console, 'error').mockImplementation(() => {});
    const onUpdate = vi.fn();
    try {
      expect(() => render(<TechnicalRequirementsTab spec={currentSpec({ technical_requirements: ['old text'] })} onUpdate={onUpdate} />))
        .toThrow('Incompatible technical requirement');
      expect(onUpdate).not.toHaveBeenCalled();
    } finally { silence.mockRestore(); }
  });
});
