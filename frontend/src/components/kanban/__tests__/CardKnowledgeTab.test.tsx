/**
 * CardKnowledgeTab - read-only card Knowledge snapshots.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CardKnowledgeTab } from '../CardKnowledgeTab';

const apiMock = vi.hoisted(() => ({
  getEffectiveResources: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

const baseCard = {
  id: 'c1',
  board_id: 'b1',
  spec_id: 's1',
  title: 'Card under test',
  description: null,
  status: 'in_progress',
  priority: 'none',
  position: 0,
  card_type: 'normal',
  knowledge_bases: [
    {
      id: 'kb_existing',
      title: 'Existing KB',
      description: 'desc',
      content: 'orig content',
      mime_type: 'text/markdown',
      source: 'copied_from_spec:s1:sk_1',
      source_kb_id: 'sk_1',
    },
  ],
} as any;

beforeEach(() => {
  document.body.innerHTML = '';
  vi.clearAllMocks();
  apiMock.getEffectiveResources.mockResolvedValue({
    resources: { architecture: [], mockup: [], knowledge_base: [] },
  });
});

describe('CardKnowledgeTab', () => {
  it('renders existing knowledge snapshots as read-only rows', () => {
    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={vi.fn()} />);

    expect(screen.getByText('Card knowledge snapshots are read-only')).toBeTruthy();
    expect(screen.getByText('Existing KB')).toBeTruthy();
    expect(screen.getByText('from spec')).toBeTruthy();
    expect(screen.getByTestId('kb-row-kb_existing')).toBeTruthy();
    expect(screen.queryByText(/New KB/i)).toBeNull();
    expect(screen.queryByText(/Link from Spec/i)).toBeNull();
    expect(screen.queryByTestId('kb-edit-kb_existing')).toBeNull();
    expect(screen.queryByTestId('kb-delete-kb_existing')).toBeNull();
  });

  it('expands a snapshot to show markdown content', () => {
    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={vi.fn()} />);

    fireEvent.click(screen.getByTestId('kb-row-kb_existing'));
    expect(screen.getByText('orig content')).toBeTruthy();
  });

  it('download button creates a Blob URL and triggers download', () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:mock-url');
    const revokeObjectURL = vi.fn();
    (URL as any).createObjectURL = createObjectURL;
    (URL as any).revokeObjectURL = revokeObjectURL;

    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={vi.fn()} />);
    fireEvent.click(screen.getByTestId('kb-download-kb_existing'));

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url');
  });

  it('shows empty state with copy guidance', () => {
    render(<CardKnowledgeTab card={{ ...baseCard, knowledge_bases: [] }} specKnowledgeBases={[]} onUpdate={vi.fn()} />);

    expect(screen.getByText('No knowledge bases')).toBeTruthy();
    expect(screen.getByText('Copy knowledge from the parent spec to populate card context.')).toBeTruthy();
  });

  it('renders inherited effective knowledge as read-only context', async () => {
    apiMock.getEffectiveResources.mockResolvedValue({
      resources: {
        architecture: [],
        mockup: [],
        knowledge_base: [
          {
            id: 'kb_parent',
            title: 'Parent KB',
            resource_type: 'knowledge_base',
            attachment_kind: 'inherited_reference',
            inherited: true,
            read_only: true,
            hydrated: true,
            source_entity_type: 'spec',
            source_entity_id: 's1',
            source_entity_title: 'Parent spec',
            resource: {
              id: 'kb_parent',
              title: 'Parent KB',
              content: 'parent content',
              mime_type: 'text/markdown',
            },
          },
        ],
      },
    });

    render(<CardKnowledgeTab card={{ ...baseCard, knowledge_bases: [] }} specKnowledgeBases={[]} onUpdate={vi.fn()} />);

    expect(await screen.findByText('Parent KB')).toBeTruthy();
    expect(screen.getByText('from spec: Parent spec')).toBeTruthy();
    expect(screen.queryByText('Copy knowledge from the parent spec to populate card context.')).toBeNull();
  });
});
