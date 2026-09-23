import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { GraphControlsPanel } from '../GraphControlsPanel';
import * as kgApi from '@/services/kg-api';

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ has: () => true, isLoading: false, error: null, ownerReviewRequired: false }),
}));

it('does not expose technical queue/retry controls even with all permissions', () => {
  render(<GraphControlsPanel
    boardId="board-a"
    filters={{ types: [], edgeTypes: [], graphLayer: 'canonical', minRelevance: 0, searchQuery: '' }}
    subView="graph" onFiltersChange={vi.fn()} onSubViewChange={vi.fn()}
    nodeCount={0} nodeLimit={100} onNodeLimitChange={vi.fn()}
  />);
  for (const name of ['Pending Queue', 'Pending Tree', 'Settings']) {
    expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
  }
  for (const name of ['Graph', 'Global Discovery', 'Audit Log', 'Privacy']) {
    expect(screen.getByRole('button', { name })).toBeInTheDocument();
  }
  for (const name of ['listPending', 'getPendingTree', 'retryPending']) {
    expect(name in kgApi).toBe(false);
  }
  expect(typeof kgApi.deleteKG).toBe('function');
});
