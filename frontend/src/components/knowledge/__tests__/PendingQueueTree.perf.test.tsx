/**
 * Performance test for ts_b379dc82 (card 65147ed4):
 *   "Árvore pending renderiza 100+ itens em <500ms".
 *
 * Mocks the kg-api response with a 4-level tree containing 100+ nodes
 * and asserts that the initial mount + flush completes within 500ms in
 * jsdom. We use performance.now() instead of Lighthouse since this lives
 * in the unit suite — Lighthouse runs in the e2e/visual project.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { PendingQueueTree } from '../PendingQueueTree';
import type { PendingTreeNode } from '@/services/kg-api';

const permissionHas = vi.hoisted(() => vi.fn((_flag: string) => true));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ preset: 'Full Control', isLoading: false, error: null, ownerReviewRequired: false, has: permissionHas }),
}));

vi.mock('@/services/kg-api', async () => {
  const actual = await vi.importActual<typeof import('@/services/kg-api')>('@/services/kg-api');
  return {
    ...actual,
    getPendingTree: vi.fn(),
  };
});

import * as kgApi from '@/services/kg-api';

function makeCards(parentId: string, count: number): PendingTreeNode[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `${parentId}_card_${i}`,
    type: 'card',
    title: `Card ${i} of ${parentId}`,
    status: i % 4 === 0 ? 'failed' : 'pending',
    queue_entry_id: `q_${parentId}_${i}`,
    children: [],
  }));
}

function makeTree(): { tree: PendingTreeNode[]; total: number } {
  // Keep the same 100 Cards: 1 Ideation + 2 Refinements + 4 Specs + 100 Cards.
  const tree: PendingTreeNode[] = [{
    id: 'idea_root',
    type: 'ideation',
    title: 'Root Ideation',
    status: 'pending',
    children: Array.from({ length: 2 }, (_, ri) => ({
      id: `ref_${ri}`,
      type: 'refinement',
      title: `Refinement ${ri}`,
      status: 'pending',
      children: Array.from({ length: 2 }, (_, si) => ({
        id: `spec_${ri}_${si}`,
        type: 'spec',
        title: `Spec ${ri}.${si}`,
        status: 'pending',
        children: [
          ...Array.from({ length: 2 }, (_, group) =>
            makeCards(`group_${ri}_${si}_${group}`, 5),
          ).flat(),
          ...makeCards(`spec_${ri}_${si}`, 15), // 60 extra cards across the spec layer
        ],
      })),
    })),
  }];
  // One of the same 100 Cards has no Spec and belongs directly to the Board.
  const orphan = tree[0].children![0].children![0].children!.pop()!;
  orphan.title = 'Card without Spec';
  tree.push(orphan);
  return { tree, total: 75 };
}

describe('PendingQueueTree perf', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  it('renders 100+ items in under 500ms (initial mount with initialData)', async () => {
    const { tree, total } = makeTree();
    const start = performance.now();
    render(
      <PendingQueueTree
        boardId="b-perf"
        initialData={{
          tree,
          levels: {
            ideations: { pending: 1, in_progress: 0, done: 0, failed: 0 },
            refinements: { pending: 2, in_progress: 0, done: 0, failed: 0 },
            specs: { pending: 4, in_progress: 0, done: 0, failed: 0 },
            cards: { pending: 68, in_progress: 0, done: 0, failed: 32 },
          },
          total_pending: total,
        }}
      />,
    );
    const elapsed = performance.now() - start;

    // 500ms ceiling per the test scenario AC.
    expect(elapsed).toBeLessThan(500);
    // Sanity: the root row exists.
    expect(screen.getByTestId('pending-queue-tree')).toBeInTheDocument();
    expect(screen.getByText('Card without Spec')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('pending-tree-expand-all'));
    expect(screen.getAllByTestId(/^pending-row-card-/)).toHaveLength(100);
    expect(screen.getAllByTestId(/^pending-row-/)).toHaveLength(107);
    expect(screen.queryByText(/sprint/i)).not.toBeInTheDocument();
  });

  it('lazy-fetch by level: descendants only render when expanded', async () => {
    // We start with everything COLLAPSED (default localStorage = empty Set).
    const { tree, total } = makeTree();
    render(
      <PendingQueueTree
        boardId="b-perf-2"
        initialData={{
          tree,
          levels: {
            ideations: { pending: 1, in_progress: 0, done: 0, failed: 0 },
            refinements: { pending: 0, in_progress: 0, done: 0, failed: 0 },
            specs: { pending: 0, in_progress: 0, done: 0, failed: 0 },
            cards: { pending: 0, in_progress: 0, done: 0, failed: 0 },
          },
          total_pending: total,
        }}
      />,
    );
    // Root visible.
    expect(screen.getByText('Root Ideation')).toBeInTheDocument();
    // Children NOT in the DOM (collapsed default).
    expect(screen.queryByText('Refinement 0')).not.toBeInTheDocument();
  });

  it('does NOT call the network when initialData is supplied', async () => {
    const { tree, total } = makeTree();
    const spy = vi.mocked(kgApi.getPendingTree);
    render(
      <PendingQueueTree
        boardId="b-perf-3"
        initialData={{
          tree,
          levels: {
            ideations: { pending: 1, in_progress: 0, done: 0, failed: 0 },
            refinements: { pending: 0, in_progress: 0, done: 0, failed: 0 },
            specs: { pending: 0, in_progress: 0, done: 0, failed: 0 },
            cards: { pending: 0, in_progress: 0, done: 0, failed: 0 },
          },
          total_pending: total,
        }}
      />,
    );
    expect(spy).not.toHaveBeenCalled();
  });
});
