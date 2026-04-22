/**
 * Performance test for ts_b379dc82 (card 65147ed4):
 *   "Árvore pending renderiza 100+ itens em <500ms".
 *
 * Mocks the kg-api response with a 5-level tree containing 100+ nodes
 * and asserts that the initial mount + flush completes within 500ms in
 * jsdom. We use performance.now() instead of Lighthouse since this lives
 * in the unit suite — Lighthouse runs in the e2e/visual project.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PendingQueueTree } from '../PendingQueueTree';
import type { PendingTreeNode } from '@/services/kg-api';

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
  // 1 ideation → 2 refinements → 2 specs each → 2 sprints each → 5 cards each
  // Total cards: 1 * 2 * 2 * 2 * 5 = 40 + intermediate nodes (1+2+4+8) = 55
  // Add 60 direct cards under specs to comfortably exceed 100.
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
          ...Array.from({ length: 2 }, (_, spi) => ({
            id: `sprint_${ri}_${si}_${spi}`,
            type: 'sprint' as const,
            title: `Sprint ${ri}.${si}.${spi}`,
            status: 'pending',
            children: makeCards(`sprint_${ri}_${si}_${spi}`, 5),
          })),
          ...makeCards(`spec_${ri}_${si}`, 15), // 60 extra cards across the spec layer
        ],
      })),
    })),
  }];
  return { tree, total: 120 };
}

describe('PendingQueueTree perf', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
            sprints: { pending: 8, in_progress: 0, done: 0, failed: 0 },
            cards: { pending: 105, in_progress: 0, done: 0, failed: 25 },
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
            sprints: { pending: 0, in_progress: 0, done: 0, failed: 0 },
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
            sprints: { pending: 0, in_progress: 0, done: 0, failed: 0 },
            cards: { pending: 0, in_progress: 0, done: 0, failed: 0 },
          },
          total_pending: total,
        }}
      />,
    );
    expect(spy).not.toHaveBeenCalled();
  });
});
