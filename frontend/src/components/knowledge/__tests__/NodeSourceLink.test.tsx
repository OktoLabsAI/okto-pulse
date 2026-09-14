import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { NodeSourceLink } from '../NodeSourceLink';
import { NodePreviewPanel } from '../NodePreviewPanel';
import { ModalStackProvider, useModalStack } from '@/contexts/ModalStackContext';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { getNodeSource, type KGNodeSource } from '@/services/kg-api';
import type { KGNode } from '@/types/knowledge-graph';

const openCard = vi.hoisted(() => vi.fn());
vi.mock('@/services/kg-api', () => ({ getNodeSource: vi.fn() }));
vi.mock('@/store/dashboard', () => ({ useDashboardStore: (select: (s: unknown) => unknown) => select({ openCardModal: openCard }) }));
const node: KGNode = { id: 'node-a', node_type: 'Decision', title: 'Graph concept', source_confidence: 1, relevance_score: 1, source_artifact_ref: 'spec:owner:decision:key' };
const resolved: KGNodeSource = { status: 'resolved', source_artifact_ref: node.source_artifact_ref!, target: { board_id: 'board-a', entity_id: 'owner', entity_type: 'spec', entity_kind: 'spec', title: 'Owner title', source_version: 7 } };
function Navigation() {
  const { stack, pop } = useModalStack();
  useEscapeToClose(pop, { enabled: stack.length > 0 });
  return <><output data-testid="stack">{JSON.stringify(stack)}</output><button onClick={pop}>Back</button></>;
}
function View({ value = node, boardId = 'board-a' }: { value?: KGNode; boardId?: string }) {
  return <ModalStackProvider><NodeSourceLink node={value} boardId={boardId} /><Navigation /></ModalStackProvider>;
}
beforeEach(() => { vi.clearAllMocks(); vi.mocked(getNodeSource).mockResolvedValue(resolved); });
afterEach(cleanup);

describe('KG source navigation shared by graph and Discovery', () => {
  it.each(['spec', 'refinement', 'ideation', 'sprint', 'story', 'card'] as const)('opens the authorized %s modal, with a node return layer', async (entityType) => {
    vi.mocked(getNodeSource).mockResolvedValue({ ...resolved, target: { ...resolved.target!, entity_type: entityType, entity_kind: entityType } });
    render(<View />);
    fireEvent.click(await screen.findByRole('button', { name: `Open ${entityType}: Owner title` }));
    expect(JSON.parse(screen.getByTestId('stack').textContent!)).toEqual([
      { type: 'kg_node', id: 'node-a', boardId: 'board-a' }, { type: entityType, id: 'owner', boardId: 'board-a' },
    ]);
    expect(openCard).toHaveBeenCalledTimes(entityType === 'card' ? 1 : 0);
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(JSON.parse(screen.getByTestId('stack').textContent!)).toEqual([{ type: 'kg_node', id: 'node-a', boardId: 'board-a' }]);
    expect(screen.getByText(/Recorded against version 7/)).toBeInTheDocument();
    expect(getNodeSource).toHaveBeenCalledTimes(1);
  });

  it.each(['task', 'test', 'bug'])('uses CardModal identity for an actual %s', async (kind) => {
    vi.mocked(getNodeSource).mockResolvedValue({ ...resolved, target: { ...resolved.target!, entity_type: 'card', entity_kind: kind } });
    render(<View />);
    fireEvent.click(await screen.findByRole('button', { name: `Open ${kind}: Owner title` }));
    expect(openCard).toHaveBeenCalledWith('owner');
    expect(screen.getByTestId('stack')).toHaveTextContent('"type":"card"');
  });

  it.each(['missing_source', 'unsupported', 'unavailable'] as const)('does not expose a destination for %s', async (status) => {
    vi.mocked(getNodeSource).mockResolvedValue({ status, target: null, source_artifact_ref: node.source_artifact_ref! });
    render(<View />);
    await waitFor(() => expect(screen.queryByText('Resolving source…')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /^Open / })).not.toBeInTheDocument();
    expect(screen.getByText(node.source_artifact_ref!)).toBeInTheDocument();
  });

  it('rejects an unexpected cross-board target', async () => {
    vi.mocked(getNodeSource).mockResolvedValue({ ...resolved, target: { ...resolved.target!, board_id: 'other' } });
    render(<View />);
    await screen.findByText(/source is unavailable/);
    expect(screen.queryByText('Owner title')).not.toBeInTheDocument();
  });

  it('cancels and ignores late results after changing board/node', async () => {
    let finish!: (result: KGNodeSource) => void;
    vi.mocked(getNodeSource).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const view = render(<View />);
    const firstSignal = vi.mocked(getNodeSource).mock.calls[0][2]!;
    vi.mocked(getNodeSource).mockResolvedValue({ ...resolved, target: { ...resolved.target!, board_id: 'board-b', title: 'New board owner' } });
    view.rerender(<View boardId="board-b" value={{ ...node, id: 'node-b' }} />);
    await screen.findByText('New board owner');
    await act(async () => finish(resolved));
    expect(firstSignal.aborted).toBe(true);
    expect(screen.queryByText('Owner title')).not.toBeInTheDocument();
  });

  it('allows a bounded retry after a failed lookup', async () => {
    vi.mocked(getNodeSource).mockRejectedValueOnce(new Error('temporary failure'));
    render(<View />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry source lookup' }));
    await screen.findByRole('button', { name: 'Open spec: Owner title' });
    expect(getNodeSource).toHaveBeenCalledTimes(2);
  });

  it('keeps the selected preview intact when Escape closes the entity layer', async () => {
    const closePreview = vi.fn();
    render(<ModalStackProvider><NodePreviewPanel node={node} boardId="board-a" onClose={closePreview} /><Navigation /></ModalStackProvider>);
    fireEvent.click(await screen.findByRole('button', { name: 'Open spec: Owner title' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(closePreview).not.toHaveBeenCalled();
    expect(screen.getByTestId('kg-preview-panel')).toBeInTheDocument();
    expect(JSON.parse(screen.getByTestId('stack').textContent!)).toHaveLength(1);
  });
});
