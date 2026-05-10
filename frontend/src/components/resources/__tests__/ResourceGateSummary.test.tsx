import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ResourceGateSummary } from '../ResourceGateSummary';
import type { ResourceGateSummary as ResourceGateSummaryType } from '@/types';

const apiMock = vi.hoisted(() => ({
  getResourceGateSummary: vi.fn(),
  markResourceNotApplicable: vi.fn(),
  clearResourceNotApplicable: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('react-hot-toast', () => ({
  default: toastMock,
}));

const baseSummary: ResourceGateSummaryType = {
  board_id: 'board-1',
  entity_type: 'ideation',
  entity_id: 'idea-1',
  blocking: true,
  warnings: [],
  missing_resources: [],
  resources: [
    {
      resource_type: 'architecture',
      state: 'missing',
      direct_count: 0,
      inherited_count: 0,
      direct_refs: [],
      inherited_refs: [],
      na_mark: null,
    },
    {
      resource_type: 'mockup',
      state: 'provided',
      direct_count: 1,
      inherited_count: 0,
      direct_refs: [{ id: 'mock-1', title: 'Flow mockup' }],
      inherited_refs: [],
      na_mark: null,
    },
    {
      resource_type: 'knowledge_base',
      state: 'not_applicable',
      direct_count: 0,
      inherited_count: 0,
      direct_refs: [],
      inherited_refs: [],
      na_mark: {
        id: 'na-1',
        active: true,
        effective: true,
        justification: 'No external knowledge is needed.',
        source_channel: 'ui',
      },
    },
  ],
};

describe('ResourceGateSummary', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getResourceGateSummary.mockResolvedValue(baseSummary);
    apiMock.markResourceNotApplicable.mockResolvedValue({
      success: true,
      mark_id: 'na-2',
      warning: null,
      summary: {
        ...baseSummary,
        resources: baseSummary.resources.map((item) =>
          item.resource_type === 'architecture'
            ? {
                ...item,
                state: 'not_applicable',
                na_mark: {
                  id: 'na-2',
                  active: true,
                  effective: true,
                  justification: 'Architecture does not apply.',
                  source_channel: 'ui',
                },
              }
            : item,
        ),
      },
    });
    apiMock.clearResourceNotApplicable.mockResolvedValue({
      success: true,
      cleared: 1,
      summary: {
        ...baseSummary,
        resources: baseSummary.resources.map((item) =>
          item.resource_type === 'knowledge_base'
            ? { ...item, state: 'missing', na_mark: null }
            : item,
        ),
      },
    });
  });

  it('renders provided, missing and N/A states and supports reversible N/A', async () => {
    render(<ResourceGateSummary boardId="board-1" entityType="ideation" entityId="idea-1" />);

    expect(await screen.findByText('Resource Gate')).toBeInTheDocument();
    expect(apiMock.getResourceGateSummary).toHaveBeenCalledWith('board-1', 'ideation', 'idea-1');

    const architecture = screen.getByTestId('resource-gate-row-architecture');
    expect(within(architecture).getByText('Missing')).toBeInTheDocument();
    fireEvent.change(within(architecture).getByPlaceholderText('Optional N/A reason'), {
      target: { value: 'Architecture does not apply.' },
    });
    fireEvent.click(within(architecture).getByRole('button', { name: /Mark N\/A/i }));

    await waitFor(() => {
      expect(apiMock.markResourceNotApplicable).toHaveBeenCalledWith('board-1', 'ideation', 'idea-1', {
        resource_type: 'architecture',
        source_channel: 'ui',
        justification: 'Architecture does not apply.',
      });
    });
    expect(await within(architecture).findByText('Architecture does not apply.')).toBeInTheDocument();

    const knowledge = screen.getByTestId('resource-gate-row-knowledge_base');
    expect(within(knowledge).getByText('No external knowledge is needed.')).toBeInTheDocument();
    fireEvent.click(within(knowledge).getByRole('button', { name: /Clear/i }));

    await waitFor(() => {
      expect(apiMock.clearResourceNotApplicable).toHaveBeenCalledWith(
        'board-1',
        'ideation',
        'idea-1',
        'knowledge_base',
        {
          source_channel: 'ui',
          reason: 'Cleared from dashboard Resource Gate summary',
        },
      );
    });
  });
});
