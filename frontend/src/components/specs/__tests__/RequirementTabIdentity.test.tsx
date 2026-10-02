import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Spec } from '@/types';
import { RulesTab } from '../RulesTab';
import { DecisionsTab } from '../DecisionsTab';
import { IntegrationRequirementsTab } from '../IntegrationRequirementsTab';
import { ObservabilityRequirementsTab } from '../ObservabilityRequirementsTab';
import { TechnicalRequirementsTab } from '../TechnicalRequirementsTab';

vi.mock('@/hooks/useCognitivePendingBadges', () => ({ useCognitivePendingBadges: () => ({ badges: {} }) }));

const currentSpec = (extra: Record<string, unknown> = {}): Spec => ({
  id: 'spec', board_id: 'board', functional_requirements: [
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
