import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { IdeationModal } from '../IdeationModal';
import type { Ideation } from '@/types';

const apiMock = vi.hoisted(() => ({
  getIdeation: vi.fn(),
  getArchitectureDesign: vi.fn(),
  listIdeationSnapshots: vi.fn(),
  listIdeationKnowledge: vi.fn(),
  listIdeationHistory: vi.fn(),
  listIdeationQA: vi.fn(),
  moveIdeation: vi.fn(),
  deleteIdeation: vi.fn(),
  updateIdeation: vi.fn(),
}));

const markdownMock = vi.hoisted(() => ({
  exportIdeation: vi.fn(() => '# ideation export'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((s: string) => s.toLowerCase().replace(/\s+/g, '-')),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({ id: 'board-1', owner_id: 'owner-1', agents: [] }),
}));

vi.mock('@/lib/exportMarkdown', () => ({
  exportIdeation: markdownMock.exportIdeation,
  downloadMarkdown: markdownMock.downloadMarkdown,
  slugify: markdownMock.slugify,
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('@/components/architecture', () => ({
  ArchitectureTab: () => <div />,
}));

vi.mock('@/components/resources/ResourceGateSummary', () => ({
  ResourceGateSummary: () => <div />,
}));

vi.mock('@/components/specs/MockupsTab', () => ({
  MockupsTab: () => <div />,
}));

vi.mock('@/components/shared/MentionInput', () => ({
  MentionInput: () => <div />,
}));

vi.mock('@/components/shared/MarkdownContent', () => ({
  MarkdownContent: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock('@/components/shared/ContextSelector', () => ({
  ContextSelector: () => <div />,
  buildIdeationItems: vi.fn(() => []),
  compileSelectedContext: vi.fn(() => ''),
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({ value, renderView, placeholder }: any) => (
    <div>{value ? renderView(value) : placeholder}</div>
  ),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const baseIdeation: Ideation = {
  id: 'ideation-1',
  board_id: 'board-1',
  title: 'My Ideation',
  description: 'An idea',
  problem_statement: 'A problem',
  proposed_approach: 'An approach',
  scope_assessment: { domains: 1, ambiguity: 1, dependencies: 1 },
  complexity: 'medium',
  screen_mockups: [],
  architecture_designs: [],
  status: 'review',
  version: 2,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-05-06T10:00:00Z',
  updated_at: '2026-05-06T10:00:00Z',
  labels: [],
  refinements: [],
  stories: [],
  specs: [],
  knowledge_bases: [],
  qa_items: [],
};

describe('IdeationModal Markdown export', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getIdeation.mockResolvedValue(baseIdeation);
    apiMock.listIdeationSnapshots.mockResolvedValue([]);
    apiMock.listIdeationKnowledge.mockResolvedValue([]);
    apiMock.listIdeationHistory.mockResolvedValue([]);
    apiMock.listIdeationQA.mockResolvedValue([]);
    apiMock.getArchitectureDesign.mockImplementation((id: string) =>
      Promise.resolve({ id, title: `${id} full`, entities: [{ id: `${id}-e`, name: 'E' }], interfaces: [], diagrams: [] }),
    );
    markdownMock.exportIdeation.mockReturnValue('# ideation export');
  });

  it('hydrates full architecture designs before export and downloads with a sanitized filename', async () => {
    apiMock.getIdeation.mockResolvedValue({
      ...baseIdeation,
      architecture_designs: [{ id: 'arch-1', title: 'Ideation arch', diagrams_count: 1 }] as any,
    });

    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    // Architecture summary is hydrated into a full design (entities + diagram payloads).
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledWith('arch-1', true));

    // exportIdeation receives the hydrated full design, not the summary.
    const lastCall = (markdownMock.exportIdeation.mock.calls.at(-1) ?? []) as any[];
    const arg = lastCall[0];
    expect(arg.architecture_designs[0]).toMatchObject({ id: 'arch-1', entities: [{ id: 'arch-1-e', name: 'E' }] });

    await waitFor(() =>
      expect(markdownMock.downloadMarkdown).toHaveBeenCalledWith('# ideation export', 'ideation_my-ideation_v2.md'),
    );
    expect(apiMock.updateIdeation).not.toHaveBeenCalled();
    expect(apiMock.moveIdeation).not.toHaveBeenCalled();
    expect(apiMock.deleteIdeation).not.toHaveBeenCalled();
  });

  it('exports without architecture calls when the ideation has no architecture designs', async () => {
    render(<IdeationModal ideationId="ideation-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Ideation');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    await waitFor(() => expect(markdownMock.exportIdeation).toHaveBeenCalled());
    expect(apiMock.getArchitectureDesign).not.toHaveBeenCalled();
    const arg = ((markdownMock.exportIdeation.mock.calls.at(-1) ?? []) as any[])[0];
    expect(arg.architecture_designs).toEqual([]);
  });
});
