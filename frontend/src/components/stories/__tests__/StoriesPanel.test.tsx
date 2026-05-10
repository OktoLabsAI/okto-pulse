import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { StoriesPanel } from '../StoriesPanel';
import type { IdeationSummary, StorySummary, TopicSummary } from '@/types';

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
  updateTopic: vi.fn(),
  deleteTopic: vi.fn(),
  mergeTopics: vi.fn(),
  listIdeations: vi.fn(),
  linkStoryToIdeation: vi.fn(),
  convertStories: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

const permissionsMock = vi.hoisted(() => ({
  has: vi.fn((_flag: string) => true),
}));

const lineageMock = vi.hoisted(() => ({
  openLineageGraph: vi.fn(),
}));

const markdownMock = vi.hoisted(() => ({
  downloadMarkdown: vi.fn(),
  exportStory: vi.fn(() => '# Story export'),
  slugify: vi.fn(() => 'stories-intake-before-ideation'),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Full Control',
    isLoading: false,
    error: null,
    has: permissionsMock.has,
  }),
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: lineageMock.openLineageGraph,
}));

vi.mock('@/lib/exportMarkdown', () => ({
  downloadMarkdown: markdownMock.downloadMarkdown,
  exportStory: markdownMock.exportStory,
  slugify: markdownMock.slugify,
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
    active_count: 1,
    archived_count: 0,
    total_associated_count: 1,
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
    apiMock.listIdeations.mockResolvedValue([]);
    apiMock.linkStoryToIdeation.mockImplementation(async () => ({
      ...stories[0],
      topic: topics[0],
      ideation_links: [
        {
          id: 'link-1',
          board_id: 'board-1',
          story_id: 'story-1',
          ideation_id: 'ideation-draft',
          created_by: 'user-1',
          created_at: '2026-05-05T00:10:00Z',
        },
      ],
    }));
    permissionsMock.has.mockReturnValue(true);
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

  it('adds standard Story modal header actions for lineage, markdown download, and expand', async () => {
    render(<StoriesPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Stories intake before ideation'));

    await screen.findByTestId('story-details-read-view');
    fireEvent.click(screen.getByTitle('Open lineage graph'));
    expect(lineageMock.openLineageGraph).toHaveBeenCalledWith('story', 'story-1');

    fireEvent.click(screen.getByTitle('Download Markdown'));
    expect(markdownMock.exportStory).toHaveBeenCalledWith(expect.objectContaining({ id: 'story-1' }));
    expect(markdownMock.downloadMarkdown).toHaveBeenCalledWith(
      '# Story export',
      'story-stories-intake-before-ideation.md',
    );

    fireEvent.click(screen.getByTitle('Expand'));
    expect(screen.getByTitle('Collapse')).toBeInTheDocument();
  });

  it('links Stories to editable Ideations through a searchable selector', async () => {
    const ideationsByStatus: Record<string, IdeationSummary[]> = {
      draft: [
        {
          id: 'ideation-draft',
          board_id: 'board-1',
          title: 'Draft ideation target',
          description: null,
          problem_statement: 'Editable target',
          complexity: null,
          status: 'draft',
          version: 1,
          assignee_id: null,
          created_by: 'user-1',
          created_at: '2026-05-05T00:00:00Z',
          updated_at: '2026-05-05T00:00:00Z',
          labels: null,
          archived: false,
        },
      ],
      review: [],
      approved: [],
      evaluating: [],
    };
    apiMock.listIdeations.mockImplementation(async (_boardId: string, status?: string) => {
      return ideationsByStatus[status || 'draft'] || [];
    });

    render(<StoriesPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Stories intake before ideation'));
    fireEvent.click(await screen.findByRole('button', { name: /Links/i }));

    expect(screen.queryByPlaceholderText('Ideation ID')).not.toBeInTheDocument();
    const search = screen.getByPlaceholderText('Search editable ideations...');
    fireEvent.focus(search);

    await waitFor(() => expect(screen.getByText('Draft ideation target')).toBeInTheDocument());

    fireEvent.change(search, { target: { value: 'draft' } });
    fireEvent.click(screen.getByText('Draft ideation target'));
    fireEvent.click(screen.getByRole('button', { name: /^Link$/i }));

    await waitFor(() => {
      expect(apiMock.linkStoryToIdeation).toHaveBeenCalledWith('story-1', 'ideation-draft');
    });
  });

  it('does not offer another Ideation link for an already linked Story', async () => {
    apiMock.getStory.mockResolvedValueOnce({
      ...stories[0],
      topic: topics[0],
      status: 'converted',
      ideation_links: [
        {
          id: 'link-existing',
          board_id: 'board-1',
          story_id: 'story-1',
          ideation_id: 'ideation-linked',
          created_by: 'user-1',
          created_at: '2026-05-05T00:05:00Z',
        },
      ],
    });

    render(<StoriesPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Stories intake before ideation'));
    fireEvent.click(await screen.findByRole('button', { name: /Links/i }));

    expect(await screen.findByText('Linked Ideation')).toBeInTheDocument();
    expect(screen.getByText('This Story already has its Ideation link. A Story can link to only one Ideation.')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Search editable ideations...')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Link$/i })).not.toBeInTheDocument();
  });

  it('uses the standard list/grid view toggle without a local refresh button', async () => {
    render(<StoriesPanel boardId="board-1" />);

    await waitFor(() => expect(screen.getByTestId('stories-list')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /refresh/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('stories-view-mode-grid'));

    expect(screen.getByTestId('stories-grid')).toBeInTheDocument();
    expect(screen.getByTestId('stories-view-mode-grid')).toHaveAttribute('aria-pressed', 'true');
  });

  it('keeps the selected Topic when the global refresh key changes', async () => {
    const { rerender } = render(<StoriesPanel boardId="board-1" refreshKey={0} />);

    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());
    fireEvent.click(screen.getAllByRole('button', { name: /Agent onboarding/i })[0]);

    await waitFor(() => {
      expect(apiMock.listStories).toHaveBeenLastCalledWith('board-1', expect.objectContaining({ topicId: 'topic-1' }));
    });

    rerender(<StoriesPanel boardId="board-1" refreshKey={1} />);

    await waitFor(() => {
      expect(apiMock.listStories).toHaveBeenLastCalledWith('board-1', expect.objectContaining({ topicId: 'topic-1' }));
    });
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

  it('edits Topic metadata and blocks deletion while Stories are associated', async () => {
    apiMock.updateTopic.mockResolvedValueOnce({
      ...topics[0],
      name: 'Agent activation',
      description: 'Updated topic description',
    });

    render(<StoriesPanel boardId="board-1" />);
    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());

    fireEvent.click(screen.getByTitle('Topic actions: Agent onboarding'));
    fireEvent.click(screen.getByText('Edit details'));

    await screen.findByText('Edit Topic');
    expect(screen.getByRole('button', { name: /Delete topic/i })).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Agent activation' } });
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Updated topic description' } });
    fireEvent.click(screen.getByRole('button', { name: /Save changes/i }));

    await waitFor(() => {
      expect(apiMock.updateTopic).toHaveBeenCalledWith('topic-1', {
        name: 'Agent activation',
        description: 'Updated topic description',
      });
    });
  });

  it('merges a Topic into another active Topic with explicit confirmation', async () => {
    const twoTopics: TopicSummary[] = [
      topics[0],
      {
        ...topics[0],
        id: 'topic-2',
        name: 'Resource Gate',
        story_count: 0,
        active_count: 0,
        total_associated_count: 0,
      },
    ];
    apiMock.listTopics.mockResolvedValue(twoTopics);
    apiMock.mergeTopics.mockResolvedValueOnce({
      source: { ...topics[0], archived: true },
      target: twoTopics[1],
      moved_count: 1,
      active_moved_count: 1,
      archived_moved_count: 0,
    });

    render(<StoriesPanel boardId="board-1" />);
    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());

    fireEvent.click(screen.getByTitle('Topic actions: Agent onboarding'));
    fireEvent.click(screen.getByText(/Merge into/i));

    await screen.findByText('Merge Topics');
    expect(screen.getByLabelText('Target Topic')).toHaveValue('topic-2');
    expect(screen.getByRole('button', { name: /Merge topics/i })).toBeDisabled();

    fireEvent.click(
      screen.getByText('I understand the source Topic will be archived and its Stories will point to the target Topic.'),
    );
    fireEvent.click(screen.getByRole('button', { name: /Merge topics/i }));

    await waitFor(() => {
      expect(apiMock.mergeTopics).toHaveBeenCalledWith('topic-1', 'topic-2');
    });
    expect(toastMock.success).toHaveBeenCalledWith('Merged 1 Stories');
  });

  it('hides Topic actions when granular Topic policies are unavailable', async () => {
    permissionsMock.has.mockImplementation((flag: string) => !flag.startsWith('topic.entity.'));

    render(<StoriesPanel boardId="board-1" />);
    await waitFor(() => expect(screen.getByText('Stories intake before ideation')).toBeInTheDocument());

    expect(screen.queryByTitle('Topic actions: Agent onboarding')).not.toBeInTheDocument();
    expect(screen.getByTitle('Missing permission: topic.entity.create')).toBeDisabled();
  });
});
