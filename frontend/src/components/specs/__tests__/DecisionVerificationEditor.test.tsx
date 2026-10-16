import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { DecisionVerificationEditor, decisionObligationOptions } from '../DecisionVerificationEditor';
import type { Spec } from '@/types';

afterEach(cleanup);
const spec = { id: 's', functional_requirements: [{ id: 'fr', title: 'Idempotency', text: 'Replay safely', status: 'active' }],
  technical_requirements: [{ id: 'old', title: 'Old policy', status: 'revoked' }],
  decisions: [{ id: 'd', title: 'Choice' }] } as unknown as Spec;

it('offers only native active obligations, never tasks or Decision cycles', () => {
  expect(decisionObligationOptions(spec)).toEqual([{ ref: 'fr:fr', label: 'Functional · Idempotency' }]);
});

it('adds inspection without losing selected obligations and makes AND explicit', () => {
  const onChange = vi.fn();
  const { rerender } = render(<DecisionVerificationEditor spec={spec} value={{ obligation_refs: ['fr:fr'] }} onChange={onChange} />);
  fireEvent.click(screen.getByLabelText('Inspect an observable condition'));
  expect(onChange).toHaveBeenCalledWith({ obligation_refs: ['fr:fr'], inspection: { condition: '', scope_refs: [{ kind: 'spec', id: 's' }] } });
  rerender(<DecisionVerificationEditor spec={spec} value={onChange.mock.calls[0][0]} onChange={onChange} />);
  expect(screen.getByText('Both paths must be satisfied.')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Expected condition'), { target: { value: 'Only the documented mock.' } });
  expect(onChange.mock.calls[1][0].inspection.condition).toBe('Only the documented mock.');
  expect(onChange.mock.calls[1][0].obligation_refs).toEqual(['fr:fr']);
});

it('leaves an empty Draft pending instead of inventing a default exemption', () => {
  render(<DecisionVerificationEditor spec={spec} value={null} onChange={vi.fn()} />);
  expect(screen.getByText(/Planning pending/)).toBeInTheDocument();
  expect(screen.getByLabelText('Inspect an observable condition')).not.toBeChecked();
});
