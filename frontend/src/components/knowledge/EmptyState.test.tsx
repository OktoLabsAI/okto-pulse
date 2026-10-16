import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EmptyState } from './EmptyState';
import * as kgApi from '@/services/kg-api';

vi.mock('./KGHelpModal', () => ({ KGHelpModal: ({ onClose }: { onClose: () => void }) => <button onClick={onClose}>Close help</button> }));

describe('Empty graph after maintenance retirement', () => {
  it('offers product guidance and help without starting jobs or polling historical progress', async () => {
    const refresh = vi.fn();
    render(<EmptyState boardId="board-1" onRefresh={refresh} />);
    expect(screen.getByText('Knowledge Graph is empty')).toBeInTheDocument();
    expect(screen.getByText(/Work on Specs and Cards/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /historical|consolidation|cancel/i })).not.toBeInTheDocument();
    expect('startHistorical' in kgApi).toBe(false);
    expect('cancelHistorical' in kgApi).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: 'Learn How It Works' }));
    expect(screen.getByRole('button', { name: 'Close help' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close help' }));
    expect('getHistoricalProgress' in kgApi).toBe(false);
    expect(refresh).not.toHaveBeenCalled();
  });
});
