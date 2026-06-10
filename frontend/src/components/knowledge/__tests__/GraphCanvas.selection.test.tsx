/**
 * Unit coverage for GraphCanvas selection wiring — Spec 8 / Sprint 4 / S4.1.
 *
 * Asserts the internal selection state machine:
 *   - no selection initially (unless initialSelectedNodeId is passed)
 *   - single-click on a node toggles selection (data-selected-id updates)
 *   - double-click on a node fires the `onSelect` callback
 *   - changing `initialSelectedNodeId` prop pushes a new selected id in
 *
 * The canvas is now rendered with Sigma (WebGL). jsdom has no WebGL
 * context, so GraphCanvas renders its accessible fallback list — which
 * intentionally implements the SAME selection semantics (click toggle,
 * double-click detail). That makes the interaction matrix directly
 * testable here, stronger than the previous React Flow version where
 * jsdom could not synthesize node clicks at all. The *visual* WebGL side
 * is validated at the browser level (Playwright).
 */

import { describe, it, expect, vi } from 'vitest';
import { render, act, fireEvent } from '@testing-library/react';
import { GraphCanvas, type GraphCanvasFilters } from '../GraphCanvas';
import type { KGNode } from '@/types/knowledge-graph';

const FILTERS: GraphCanvasFilters = {
  types: [],
  edgeTypes: [],
  minRelevance: 0,
  searchQuery: '',
};

const NODE: KGNode = {
  id: 'selected-42',
  title: 'Selected node',
  content: '',
  source_confidence: 1,
  relevance_score: 0.85,
  node_type: 'Decision',
  created_at: '2026-04-16T00:00:00',
};

function renderCanvas(props: Partial<React.ComponentProps<typeof GraphCanvas>> = {}) {
  return render(
    <GraphCanvas
      nodes={[NODE]}
      edges={[]}
      filters={FILTERS}
      {...props}
    />,
  );
}

describe('GraphCanvas — selection wiring (S4.1 / AC-4)', () => {
  it('starts with no selection when initialSelectedNodeId is null', () => {
    const { container } = renderCanvas();
    const canvas = container.querySelector('[data-testid="kg-canvas"]');
    expect(canvas?.getAttribute('data-selected-id')).toBe('');
  });

  it('reflects initialSelectedNodeId on mount', () => {
    const { container } = renderCanvas({ initialSelectedNodeId: 'selected-42' });
    const canvas = container.querySelector('[data-testid="kg-canvas"]');
    expect(canvas?.getAttribute('data-selected-id')).toBe('selected-42');
  });

  it('updates when initialSelectedNodeId prop changes', () => {
    const { container, rerender } = renderCanvas({ initialSelectedNodeId: null });
    rerender(
      <GraphCanvas
        nodes={[NODE]}
        edges={[]}
        filters={FILTERS}
        initialSelectedNodeId="selected-42"
      />,
    );
    const canvas = container.querySelector('[data-testid="kg-canvas"]');
    expect(canvas?.getAttribute('data-selected-id')).toBe('selected-42');
  });

  it('does not invoke onSelect for a single click (single-click is reserved for toggle)', () => {
    const onSelect = vi.fn();
    const { container } = renderCanvas({ onSelect });
    const nodeButton = container.querySelector('[data-testid="kg-node-decision"]');
    expect(nodeButton).not.toBeNull();
    fireEvent.click(nodeButton!);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('single-click toggles selection on and off (AC-4)', () => {
    const onNodeClick = vi.fn();
    const { container } = renderCanvas({ onNodeClick });
    const canvas = container.querySelector('[data-testid="kg-canvas"]');
    const nodeButton = container.querySelector('[data-testid="kg-node-decision"]');

    fireEvent.click(nodeButton!);
    expect(canvas?.getAttribute('data-selected-id')).toBe('selected-42');
    expect(onNodeClick).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'selected-42' }));

    fireEvent.click(nodeButton!);
    expect(canvas?.getAttribute('data-selected-id')).toBe('');
  });

  it('double-click fires onSelect with the KG node (AC-4)', () => {
    const onSelect = vi.fn();
    const { container } = renderCanvas({ onSelect });
    const nodeButton = container.querySelector('[data-testid="kg-node-decision"]');
    fireEvent.doubleClick(nodeButton!);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'selected-42' }));
  });

  it('renders the accessible fallback (not a crash) when WebGL is unavailable', () => {
    const { container } = renderCanvas();
    expect(container.querySelector('[data-testid="kg-webgl-fallback"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="kg-canvas"]')?.getAttribute('data-renderer')).toBe(
      'fallback',
    );
  });

  it('renders the empty-state fallback when filters exclude every node', () => {
    const { container } = renderCanvas({
      filters: { ...FILTERS, minRelevance: 1.01 },
    });
    expect(container.textContent).toMatch(
      /No nodes (match the current filters|match the relevance filter)/,
    );
    expect(container.querySelector('[data-testid="kg-canvas"]')).toBeNull();
  });

  it('prop-driven selection can be cleared externally by passing null', () => {
    const { container, rerender } = renderCanvas({ initialSelectedNodeId: 'selected-42' });
    act(() => {
      rerender(
        <GraphCanvas
          nodes={[NODE]}
          edges={[]}
          filters={FILTERS}
          initialSelectedNodeId={null}
        />,
      );
    });
    const canvas = container.querySelector('[data-testid="kg-canvas"]');
    expect(canvas?.getAttribute('data-selected-id')).toBe('');
  });
});
