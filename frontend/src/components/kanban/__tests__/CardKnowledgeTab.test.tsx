/**
 * CardKnowledgeTab — TC-3 (TS3) covering CRUD on a card's knowledge bases.
 * Verifies: add, list/expand, edit, delete, link-from-spec, and the new
 * markdown download (Blob URL is created and revoked).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CardKnowledgeTab } from '../CardKnowledgeTab';

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
    { id: 'kb_existing', title: 'Existing KB', description: 'desc', content: 'orig content', mime_type: 'text/markdown' },
  ],
} as any;

beforeEach(() => {
  // toast.success/error use document.body
  document.body.innerHTML = '';
});

describe('CardKnowledgeTab', () => {
  it('renders existing knowledge bases', () => {
    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={vi.fn()} />);
    expect(screen.getByText('Existing KB')).toBeTruthy();
    expect(screen.getByTestId('kb-row-kb_existing')).toBeTruthy();
  });

  it('adds a new KB through the form (calls onUpdate with appended array)', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByText(/New KB/i));
    const inputs = screen.getAllByRole('textbox');
    // First input = title, then the textarea
    fireEvent.change(inputs[0], { target: { value: 'New entry' } });
    fireEvent.change(inputs[1], { target: { value: 'New body content' } });
    fireEvent.click(screen.getByText('Add'));
    await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    const next = onUpdate.mock.calls[0][0];
    expect(next).toHaveLength(2);
    expect(next[1].title).toBe('New entry');
    expect(next[1].content).toBe('New body content');
    expect(next[1].source).toBe('manual');
  });

  it('deletes a KB (calls onUpdate without that id)', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByTestId('kb-delete-kb_existing'));
    await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    const next = onUpdate.mock.calls[0][0];
    expect(next.find((k: any) => k.id === 'kb_existing')).toBeUndefined();
  });

  it('edits a KB in place (calls onUpdate with updated content)', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByTestId('kb-edit-kb_existing'));
    const titleInput = screen.getByTestId('kb-edit-title-kb_existing');
    const contentInput = screen.getByTestId('kb-edit-content-kb_existing');
    fireEvent.change(titleInput, { target: { value: 'Renamed' } });
    fireEvent.change(contentInput, { target: { value: 'updated body' } });
    fireEvent.click(screen.getByTestId('kb-edit-save-kb_existing'));
    await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    const next = onUpdate.mock.calls[0][0];
    const updated = next.find((k: any) => k.id === 'kb_existing');
    expect(updated.title).toBe('Renamed');
    expect(updated.content).toBe('updated body');
  });

  it('links a KB from the parent spec (preserves source_id and tags source=spec)', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    render(
      <CardKnowledgeTab
        card={{ ...baseCard, knowledge_bases: [] }}
        specKnowledgeBases={[
          { id: 'sk_1', title: 'From Spec', description: 'd', content: 'spec body', mime_type: 'text/markdown' },
        ]}
        onUpdate={onUpdate}
      />,
    );
    fireEvent.click(screen.getByText(/Link from Spec/));
    fireEvent.click(screen.getByText('+ Link'));
    await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    const next = onUpdate.mock.calls[0][0];
    expect(next).toHaveLength(1);
    expect(next[0].source).toBe('spec');
    expect(next[0].source_id).toBe('sk_1');
    expect(next[0].title).toBe('From Spec');
  });

  it('download button creates a Blob URL and triggers <a download>', () => {
    // jsdom does not implement createObjectURL — stub it.
    const createObjectURL = vi.fn().mockReturnValue('blob:mock-url');
    const revokeObjectURL = vi.fn();
    (URL as any).createObjectURL = createObjectURL;
    (URL as any).revokeObjectURL = revokeObjectURL;

    render(<CardKnowledgeTab card={baseCard} specKnowledgeBases={[]} onUpdate={vi.fn()} />);
    fireEvent.click(screen.getByTestId('kb-download-kb_existing'));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url');
  });

  it('shows empty state with no KBs and no open forms', () => {
    render(<CardKnowledgeTab card={{ ...baseCard, knowledge_bases: [] }} specKnowledgeBases={[]} onUpdate={vi.fn()} />);
    expect(screen.getByText('No knowledge bases')).toBeTruthy();
  });
});
