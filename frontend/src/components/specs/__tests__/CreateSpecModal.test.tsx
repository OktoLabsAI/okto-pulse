import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CreateSpecModal } from '../CreateSpecModal';

const api = vi.hoisted(() => ({ createSpec: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

describe('CreateSpecModal current requirement contract', () => {
  beforeEach(() => { vi.clearAllMocks(); api.createSpec.mockResolvedValue({ id: 'spec-created' }); });

  it('authors FR/TR/AC as objects and sends the selected delivery context', async () => {
    const created = vi.fn();
    render(<CreateSpecModal boardId="board" onClose={vi.fn()} onCreated={created} />);
    fireEvent.change(screen.getByPlaceholderText('What needs to be built?'), { target: { value: 'New Spec' } });
    fireEvent.change(screen.getByLabelText('Delivery context *'), { target: { value: 'greenfield' } });
    for (const [placeholder, value] of [
      [/functional requirement/i, 'FR text'],
      [/technical constraint/i, 'TR text'],
      [/acceptance criteri/i, 'AC text'],
    ] as const) {
      const input = screen.getByPlaceholderText(placeholder);
      fireEvent.change(input, { target: { value } });
      fireEvent.keyDown(input, { key: 'Enter' });
    }
    fireEvent.submit(screen.getByPlaceholderText('What needs to be built?').closest('form')!);
    await waitFor(() => expect(created).toHaveBeenCalledWith({ id: 'spec-created' }));
    expect(api.createSpec).toHaveBeenCalledWith('board', expect.objectContaining({
      title: 'New Spec', delivery_context: 'greenfield',
      functional_requirements: [{ text: 'FR text' }],
      technical_requirements: [{ text: 'TR text' }],
      acceptance_criteria: [{ text: 'AC text' }],
    }));
  });

  it('requires delivery context without inventing a default', () => {
    render(<CreateSpecModal boardId="board" onClose={vi.fn()} onCreated={vi.fn()} />);
    const title = screen.getByPlaceholderText('What needs to be built?');
    fireEvent.change(title, { target: { value: 'New Spec' } });
    fireEvent.submit(title.closest('form')!);
    expect(api.createSpec).not.toHaveBeenCalled();
  });
});
