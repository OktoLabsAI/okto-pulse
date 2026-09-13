import { act, renderHook } from '@testing-library/react';
import type { Node } from '@xyflow/react';
import { describe, expect, it } from 'vitest';
import { useMeasuredLineageNodes } from '../useMeasuredLineageNodes';

describe('controlled lineage measurements', () => {
  it('publishes dimensions for the minimap without permitting drag, deletion or selection changes', () => {
    const node: Node = { id: 'a', position: { x: 400, y: 20 }, data: {}, selected: false };
    const { result, rerender } = renderHook(({ nodes }) => useMeasuredLineageNodes(nodes), { initialProps: { nodes: [node] } });
    act(() => result.current.onNodesChange([
      { type: 'dimensions', id: 'a', dimensions: { width: 236, height: 117 } },
      { type: 'position', id: 'a', position: { x: -500, y: -200 } },
      { type: 'select', id: 'a', selected: true },
      { type: 'remove', id: 'a' },
    ]));
    expect(result.current.nodes).toEqual([{ ...node, measured: { width: 236, height: 117 } }]);
    const previous = result.current.nodes;
    act(() => result.current.onNodesChange([{ type: 'dimensions', id: 'a', dimensions: { width: 236, height: 117 } }]));
    expect(result.current.nodes).toBe(previous);
    rerender({ nodes: [{ ...node, selected: true, position: { x: 800, y: 50 } }] });
    expect(result.current.nodes[0]).toMatchObject({ selected: true, position: { x: 800, y: 50 }, measured: { width: 236, height: 117 } });
  });
});
