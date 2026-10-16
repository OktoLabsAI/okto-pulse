import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SpecKnowledgeResources } from '../SpecKnowledgeResources';

const api = vi.hoisted(() => ({ createSpecKnowledge: vi.fn(), deleteSpecKnowledge: vi.fn(), listSpecKnowledge: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@/components/resources/KnowledgeWorkspace', () => ({
  KnowledgeWorkspace: ({ active, refreshKey, onDelete, canDelete }: { active: boolean; refreshKey: string; onDelete: (id: string) => Promise<boolean>; canDelete: () => boolean }) =>
    <div data-testid="knowledge-workspace" data-active={active} data-revision={refreshKey}>
      <button disabled={!canDelete()} onClick={() => { void onDelete('kb-created'); }}>Delete known item</button>
    </div>,
}));
const props = { boardId: 'board', specId: 'spec', active: true, readCurrent: true, revision: 0 };
function fill() {
  fireEvent.click(screen.getByRole('button', { name: 'Add Knowledge' }));
  fireEvent.change(screen.getByLabelText('Knowledge title'), { target: { value: 'Local knowledge' } });
  fireEvent.change(screen.getByLabelText('Knowledge content'), { target: { value: 'Local content' } });
}

describe('Spec knowledge operations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createSpecKnowledge.mockResolvedValue({ id: 'kb-created' });
    api.listSpecKnowledge.mockResolvedValue([{ id: 'kb-created', title: 'Local knowledge' }]);
    api.deleteSpecKnowledge.mockResolvedValue(undefined);
  });
  it('keeps drafts across revisions and hidden panels without allowing hidden writes', () => {
    const onReload = vi.fn();
    const view = render(<SpecKnowledgeResources {...props} onReload={onReload} />);
    fill();
    view.rerender(<SpecKnowledgeResources {...props} revision={2} active={false} onReload={onReload} />);
    expect(screen.getByLabelText('Knowledge title')).toHaveValue('Local knowledge');
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
    expect(screen.getByTestId('knowledge-workspace')).toHaveAttribute('data-active', 'false');
    expect(screen.getByTestId('knowledge-workspace')).toHaveAttribute('data-revision', '2:0');
  });
  it('retains a confirmed creation through failed parent reads without a second POST', async () => {
    const onReload = vi.fn().mockRejectedValueOnce(new Error('Parent unavailable')).mockResolvedValue(undefined);
    render(<SpecKnowledgeResources {...props} onReload={onReload} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await screen.findByText('Parent unavailable');
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved knowledge' }));
    await waitFor(() => expect(screen.queryByLabelText('Knowledge title')).not.toBeInTheDocument());
    expect(api.createSpecKnowledge).toHaveBeenCalledTimes(1);
    expect(onReload).toHaveBeenCalledTimes(2);
  });
  it('does not clear a confirmed creation when its authoritative ID is absent', async () => {
    api.listSpecKnowledge.mockResolvedValue([]);
    const onReload = vi.fn();
    render(<SpecKnowledgeResources {...props} onReload={onReload} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await screen.findByText('The saved knowledge item is not available in the current read yet.');
    expect(onReload).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Knowledge content')).toHaveValue('Local content');
  });
  it('blocks uncertain creation until inspection and explicit discard, without guessing by title', async () => {
    api.createSpecKnowledge.mockRejectedValue(new TypeError('Response lost'));
    render(<SpecKnowledgeResources {...props} onReload={vi.fn().mockResolvedValue(undefined)} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await screen.findByText('Response lost');
    expect(screen.getByRole('button', { name: 'I reviewed knowledge; discard local attempt' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload current knowledge' }));
    await screen.findByText('Local knowledge · kb-created');
    expect(screen.getByLabelText('Knowledge title')).toHaveValue('Local knowledge');
    fireEvent.click(screen.getByRole('button', { name: 'I reviewed knowledge; discard local attempt' }));
    expect(api.createSpecKnowledge).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('Knowledge title')).not.toBeInTheDocument();
  });
  it('recovers deletion by reading, without another DELETE', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      api.listSpecKnowledge.mockRejectedValueOnce(new Error('List unavailable')).mockResolvedValue([]);
      render(<SpecKnowledgeResources {...props} onReload={vi.fn().mockResolvedValue(undefined)} />);
      fireEvent.click(screen.getByRole('button', { name: 'Delete known item' }));
      await screen.findByText('List unavailable');
      expect(screen.getByRole('button', { name: 'Delete known item' })).toBeDisabled();
      fireEvent.click(screen.getByRole('button', { name: 'Reload saved knowledge' }));
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Reload saved knowledge' })).not.toBeInTheDocument());
      expect(api.deleteSpecKnowledge).toHaveBeenCalledTimes(1);
    } finally { confirm.mockRestore(); }
  });
  it('clears protected drafts on access denial', async () => {
    api.createSpecKnowledge.mockRejectedValue(Object.assign(new Error('Denied'), { status: 403 }));
    render(<SpecKnowledgeResources {...props} onReload={vi.fn()} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await screen.findByText('Knowledge access is no longer available.');
    expect(screen.queryByLabelText('Knowledge content')).not.toBeInTheDocument();
    expect(screen.queryByTestId('knowledge-workspace')).not.toBeInTheDocument();
  });
  it('discards a late creation after switching to another Spec', async () => {
    let finish!: (value: { id: string }) => void;
    api.createSpecKnowledge.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const onReload = vi.fn();
    const view = render(<SpecKnowledgeResources {...props} onReload={onReload} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    view.rerender(<SpecKnowledgeResources {...props} specId="other" onReload={onReload} />);
    await act(async () => finish({ id: 'old-created' }));
    expect(onReload).not.toHaveBeenCalled();
    expect(api.listSpecKnowledge).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Knowledge title')).not.toBeInTheDocument();
  });
});
