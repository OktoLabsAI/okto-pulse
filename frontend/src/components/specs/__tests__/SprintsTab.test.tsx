import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SprintSummary } from '@/types';
import { SprintsTab } from '../SprintsTab';

const apiMock = vi.hoisted(() => ({
  listSprints: vi.fn(),
  createSprint: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/components/sprints/SprintModal', () => ({
  SprintModal: () => null,
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

function sprint(overrides: Partial<SprintSummary> = {}): SprintSummary {
  return {
    id: 'sprint-1',
    spec_id: 'spec-1',
    board_id: 'board-1',
    title: 'Nested sprint',
    description: null,
    objective: null,
    expected_outcome: null,
    status: 'active',
    lane_type: 'normal',
    origin_sprint_id: null,
    origin_bug_id: null,
    normal_sprint_created: false,
    spec_version: 3,
    start_date: null,
    end_date: null,
    test_scenario_ids: [],
    business_rule_ids: [],
    version: 1,
    labels: [],
    created_by: 'user-1',
    created_at: '2026-08-14T10:00:00Z',
    updated_at: '2026-08-14T10:00:00Z',
    archived: false,
    ...overrides,
  };
}

describe('SprintsTab open Q&A badges', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders positive counts and omits zero or unavailable counts', async () => {
    apiMock.listSprints.mockResolvedValue([
      sprint({ id: 'sprint-open', title: 'Nested sprint with Q&A', open_qa_count: 3 }),
      sprint({ id: 'sprint-clear', title: 'Nested sprint without Q&A', open_qa_count: 0 }),
      sprint({ id: 'sprint-denied', title: 'Nested sprint without projection' }),
    ]);

    render(<SprintsTab specId="spec-1" boardId="board-1" />);

    await screen.findByText('Nested sprint without projection');
    expect(screen.getAllByTestId('qa-open-badge')).toHaveLength(1);
    expect(screen.getByLabelText('3 unanswered questions')).toHaveTextContent('3 open Q&A');
  });
});
