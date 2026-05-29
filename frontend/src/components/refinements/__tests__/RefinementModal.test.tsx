import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RefinementModal } from '../RefinementModal';
import type { Refinement } from '@/types';

const apiMock = vi.hoisted(() => ({
  getRefinement: vi.fn(),
  getRefinementKnowledge: vi.fn(),
  getArchitectureDesign: vi.fn(),
  listRefinementSnapshots: vi.fn(),
  listRefinementHistory: vi.fn(),
  listRefinementQA: vi.fn(),
  moveRefinement: vi.fn(),
  deleteRefinement: vi.fn(),
  updateRefinement: vi.fn(),
}));

const markdownMock = vi.hoisted(() => ({
  exportRefinement: vi.fn(() => '# refinement export'),
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
  exportRefinement: markdownMock.exportRefinement,
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

vi.mock('@/components/ideations/IdeationModal', () => ({
  IdeationModal: () => <div />,
}));

vi.mock('@/components/shared/MentionInput', () => ({
  MentionInput: () => <div />,
}));

vi.mock('@/components/shared/MarkdownContent', () => ({
  MarkdownContent: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock('@/components/shared/ContextSelector', () => ({
  ContextSelector: () => <div />,
  buildRefinementItems: vi.fn(() => []),
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({ value, renderView, placeholder }: any) => (
    <div>{value ? renderView(value) : placeholder}</div>
  ),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const baseRefinement: Refinement = {
  id: 'refinement-1',
  ideation_id: 'ideation-1',
  board_id: 'board-1',
  title: 'My Refinement',
  description: 'A refinement',
  in_scope: ['in'],
  out_of_scope: ['out'],
  analysis: 'analysis',
  decisions: ['decision'],
  screen_mockups: [],
  architecture_designs: [],
  status: 'review',
  version: 3,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-05-06T10:00:00Z',
  updated_at: '2026-05-06T10:00:00Z',
  labels: [],
  specs: [],
  qa_items: [],
  knowledge_bases: [],
};

describe('RefinementModal Markdown export', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getRefinement.mockResolvedValue(baseRefinement);
    apiMock.getRefinementKnowledge.mockImplementation((_rid: string, kbId: string) =>
      Promise.resolve({ id: kbId, content: 'kb content' }),
    );
    apiMock.listRefinementSnapshots.mockResolvedValue([]);
    apiMock.listRefinementHistory.mockResolvedValue([]);
    apiMock.listRefinementQA.mockResolvedValue([]);
    apiMock.getArchitectureDesign.mockImplementation((id: string) =>
      Promise.resolve({ id, title: `${id} full`, entities: [{ id: `${id}-e`, name: 'E' }], interfaces: [], diagrams: [] }),
    );
    markdownMock.exportRefinement.mockReturnValue('# refinement export');
  });

  it('hydrates full architecture designs (alongside knowledge bases) before export', async () => {
    apiMock.getRefinement.mockResolvedValue({
      ...baseRefinement,
      architecture_designs: [{ id: 'arch-1', title: 'Refinement arch', diagrams_count: 1 }] as any,
      knowledge_bases: [{ id: 'kb-1', title: 'KB' }] as any,
    });

    render(<RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Refinement');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    // Architecture summary is hydrated into a full design (entities + diagram payloads).
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledWith('arch-1', true));
    // Knowledge bases are still hydrated too (existing behavior preserved).
    expect(apiMock.getRefinementKnowledge).toHaveBeenCalledWith('refinement-1', 'kb-1');

    // exportRefinement receives the hydrated full design, not the summary.
    const lastCall = (markdownMock.exportRefinement.mock.calls.at(-1) ?? []) as any[];
    const arg = lastCall[0];
    expect(arg.architecture_designs[0]).toMatchObject({ id: 'arch-1', entities: [{ id: 'arch-1-e', name: 'E' }] });

    await waitFor(() =>
      expect(markdownMock.downloadMarkdown).toHaveBeenCalledWith('# refinement export', 'refinement_my-refinement_v3.md'),
    );
    expect(apiMock.updateRefinement).not.toHaveBeenCalled();
    expect(apiMock.moveRefinement).not.toHaveBeenCalled();
    expect(apiMock.deleteRefinement).not.toHaveBeenCalled();
  });

  it('exports without architecture calls when the refinement has no architecture designs', async () => {
    render(<RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Refinement');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    await waitFor(() => expect(markdownMock.exportRefinement).toHaveBeenCalled());
    expect(apiMock.getArchitectureDesign).not.toHaveBeenCalled();
    const arg = ((markdownMock.exportRefinement.mock.calls.at(-1) ?? []) as any[])[0];
    expect(arg.architecture_designs).toEqual([]);
  });
});
