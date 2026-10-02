import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ApiContract, Spec } from '@/types';
import { ContractsTab } from '../ContractsTab';

const contract = (patch: Partial<ApiContract> = {}): ApiContract => ({
  id: 'api-current', contract_type: 'http', method: 'GET', path: '/orders',
  description: 'Read orders', request_body: null, response_success: null,
  response_errors: null, linked_requirements: ['fr-order'], linked_rules: null,
  linked_task_ids: ['task-current'], status: 'active', notes: null, ...patch,
});
const spec = (contracts: ApiContract[] = []): Spec => ({
  id: 'spec', api_contracts: contracts,
  functional_requirements: [{ id: 'fr-order', text: 'Return orders' }], business_rules: [],
} as unknown as Spec);

describe('current API contracts', () => {
  it('authors HTTP with exact requirement IDs and no legacy methods', () => {
    const onUpdate = vi.fn();
    render(<ContractsTab spec={spec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add API Contract' }));
    expect(screen.queryByRole('option', { name: 'TOOL' })).toBeNull();
    fireEvent.change(screen.getByPlaceholderText('/api/v1/resource or component name'), { target: { value: '/orders' } });
    fireEvent.change(screen.getByPlaceholderText('Description of this endpoint/interface'), { target: { value: 'Read orders' } });
    fireEvent.click(screen.getByRole('button', { name: 'FR1: Return orders' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add Contract' }));
    expect(onUpdate.mock.calls[0][0][0]).toMatchObject({
      contract_type: 'http', method: 'GET', path: '/orders', linked_requirements: ['fr-order'],
    });
  });

  it.each(['in_process', 'grpc', 'event'])('authors %s explicitly without an HTTP path', (kind) => {
    const onUpdate = vi.fn();
    render(<ContractsTab spec={spec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add API Contract' }));
    fireEvent.change(screen.getByLabelText('Contract type'), { target: { value: kind } });
    expect(screen.queryByLabelText('HTTP method')).toBeNull();
    fireEvent.change(screen.getByPlaceholderText('Description of this endpoint/interface'), { target: { value: 'Notify orders' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Contract' }));
    expect(onUpdate.mock.calls[0][0][0]).toMatchObject({ contract_type: kind, method: null, path: null });
  });

  it('preserves task links and inactive history when editing', () => {
    const onUpdate = vi.fn();
    const inactive = contract({ id: 'previous', status: 'superseded' });
    render(<ContractsTab spec={spec([contract(), inactive])} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByTitle('Edit'));
    fireEvent.change(screen.getByPlaceholderText('Description of this endpoint/interface'), { target: { value: 'Read current orders' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onUpdate.mock.calls[0][0]).toEqual([
      expect.objectContaining({ id: 'api-current', linked_task_ids: ['task-current'], linked_requirements: ['fr-order'] }),
      inactive,
    ]);
  });

  it('requires an HTTP path and respects creation permission', () => {
    const onUpdate = vi.fn();
    const view = render(<ContractsTab spec={spec()} onUpdate={onUpdate} canCreate={false} />);
    expect(screen.getByRole('button', { name: 'Add API Contract' })).toBeDisabled();
    view.rerender(<ContractsTab spec={spec()} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add API Contract' }));
    fireEvent.change(screen.getByPlaceholderText('Description of this endpoint/interface'), { target: { value: 'Read orders' } });
    expect(screen.getByRole('button', { name: 'Add Contract' })).toBeDisabled();
    expect(onUpdate).not.toHaveBeenCalled();
  });
});
