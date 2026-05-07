import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { StoriesPanel } from '../StoriesPanel';
import type { StorySummary, TopicSummary } from '@/types';

const apiMock = vi.hoisted(() => ({
  listTopics: vi.fn(),
  listStories: vi.fn(),
  createTopic: vi.fn(),
  getStory: vi.fn(),
  createStory: vi.fn(),
  updateStory: vi.fn(),
  moveStory: vi.fn(),
  archiveStory: vi.fn(),
  restoreStory: vi.fn(),
  linkStoryToIdeation: vi.fn(),
  convertStories: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: toastMock,
}));

const topics: TopicSummary[] = [
  {
    id: 'topic-1',
    board_id: 'board-1',
    name: 'Agent onboarding',
    description: null,
    archived: false,
    story_count: 1,
    created_by: 'user-1',
    created_at: '2026-05-05T00:00:00Z',
    updated_at: '2026-05-05T00:00:00Z',
  },
];

const stories: StorySummary[] = [
  {
    id: 'story-1',
    board_id: 'board-1',
    topic_id: 'topic-1',
    title: 'Stories intake before ideation',
    description: 'As a maintainer, I want raw needs grouped before ideation.',
    actor: 'maintainer',
    goal: 'group raw needs',
    benefit: 'less noisy ideations',
    labels: ['intake'],
    status: 'ready',
    assignee_id: null,
    created_by: 'user-1',
    created_at: '2026-05-05T00:00:00Z',
    updated_at: '2026-05-05T00:00:00Z',
    archived: false,
    pre_archive_status: null,
    screen_mockups: [],
    ideation_links: [],
  },
];

describe('StoriesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    apiMock.listTopics.mockResolvedValue(topics);
    apiMock.listStories.mockResolvedValue(stories);
    apiMock.getStory.mockResolvedValue({
      ...stories[0],
      topic: topics[0],
    });
  });

  it('renders Stories before Ideation work with Topic grouping and modal mockups tab', async () => {
    render(<StoriesPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());
    expect(screen.getAllByText('Agent onboarding').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: /New Story/i }));
    await waitFor(() => expect(screen.getAllByText('New Story').length).toBeGreaterThan(1));

    fireEvent.click(screen.getByText('Mockups'));
    expect(screen.getByText('No screen mockups yet')).toBeInTheDocument();
  });

  it('opens existing Story details in read mode before inline editing description', async () => {
    render(<StoriesPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Stories intake before ideation'));

    const details = await screen.findByTestId('story-details-read-view');
    const descriptionField = within(details).getByTestId('story-description-field');

    expect(within(descriptionField).getByText('As a maintainer, I want raw needs grouped before ideation.')).toBeInTheDocument();
    expect(within(descriptionField).queryByRole('textbox')).not.toBeInTheDocument();

    fireEvent.click(within(descriptionField).getByText('As a maintainer, I want raw needs grouped before ideation.'));

    expect(within(descriptionField).getByRole('textbox')).toHaveValue('As a maintainer, I want raw needs grouped before ideation.');
  });

  it('uses the standard list/grid view toggle without a local refresh button', async () => {
    render(<StoriesPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByTestId('stories-list')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /refresh/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('stories-view-mode-grid'));

    expect(screen.getByTestId('stories-grid')).toBeInTheDocument();
    expect(screen.getByTestId('stories-view-mode-grid')).toHaveAttribute('aria-pressed', 'true');
  });

  it('surfaces backend Topic uniqueness errors from the Topic form', async () => {
    apiMock.createTopic.mockRejectedValueOnce(new Error('Topic name already exists in this board'));

    render(<StoriesPanel boardId="board-1" />);
    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());

    fireEvent.click(screen.getByTitle('New Topic'));
    fireEvent.change(screen.getByPlaceholderText('Topic name'), { target: { value: 'Agent onboarding' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith('Topic name already exists in this board');
    });
  });
});
