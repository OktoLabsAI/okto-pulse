/**
 * Unit coverage for the force layout helpers — Spec 8 / S3.6.
 *
 * Covers ts_385e02af: the chargeStrength formula
 *   strength = -Math.max(400, 120 * (edgeCount / Math.max(nodeCount, 1)))
 * which floors repulsion at -400 for sparse graphs and scales with edge
 * density for dense ones. Also asserts no division-by-zero when a graph
 * is rendered with zero nodes (transient state during pagination).
 *
 * computeForceLayout itself is smoke-tested: given N nodes and an edge
 * list, every node should receive a finite (x, y) coordinate pair.
 */

import { describe, it, expect } from 'vitest';
import {
  computeChargeStrength,
  computeForceLayout,
} from '@/components/knowledge/graph/forceLayout';
import type { KGNode, KGEdge } from '@/types/knowledge-graph';

function makeNode(id: string, overrides: Partial<KGNode> = {}): KGNode {
  return {
    id,
    title: `Node ${id}`,
    content: '',
    source_confidence: 0.8,
    validation_status: 'corroborated',
    node_type: 'Decision',
    created_at: '2026-04-16T10:00:00',
    ...overrides,
  };
}

function makeEdge(source: string, target: string): KGEdge {
  return {
    id: `${source}-${target}`,
    source,
    target,
    edge_type: 'supports',
    confidence: 0.9,
    created_at: '2026-04-16T10:00:00',
  };
}

describe('computeChargeStrength (ts_385e02af)', () => {
  it('sparse graph (10 nodes, 3 edges) returns the -400 floor', () => {
    expect(computeChargeStrength(10, 3)).toBe(-400);
  });

  it('dense graph (100 nodes, 200 edges) still clamps at the -400 floor because 120*2=240 < 400', () => {
    expect(computeChargeStrength(100, 200)).toBe(-Math.max(400, 240));
    expect(computeChargeStrength(100, 200)).toBe(-400);
  });

  it('very dense graph scales beyond the floor (5 nodes, 50 edges → 120*10=1200)', () => {
    expect(computeChargeStrength(5, 50)).toBe(-1200);
  });

  it('zero nodes does not divide by zero — uses Math.max(nodeCount, 1) as denominator', () => {
    expect(() => computeChargeStrength(0, 5)).not.toThrow();
    expect(computeChargeStrength(0, 5)).toBe(-Math.max(400, 120 * 5));
    expect(computeChargeStrength(0, 5)).toBe(-600);
  });

  it('zero edges collapses the density term to 0 and returns -400', () => {
    expect(computeChargeStrength(10, 0)).toBe(-400);
  });
});

describe('computeForceLayout', () => {
  it('assigns a finite {x, y} to every node in the input', () => {
    const nodes = Array.from({ length: 12 }, (_, i) => makeNode(`n${i}`));
    const edges: KGEdge[] = [
      makeEdge('n0', 'n1'),
      makeEdge('n1', 'n2'),
      makeEdge('n2', 'n3'),
      makeEdge('n4', 'n5'),
    ];

    const positions = computeForceLayout(nodes, edges);

    expect(positions.size).toBe(nodes.length);
    for (const n of nodes) {
      const pos = positions.get(n.id);
      expect(pos).toBeDefined();
      expect(Number.isFinite(pos!.x)).toBe(true);
      expect(Number.isFinite(pos!.y)).toBe(true);
    }
  });

  it('empty input returns an empty map (no crash)', () => {
    const positions = computeForceLayout([], []);
    expect(positions.size).toBe(0);
  });

  it('ignores edges referencing unknown nodes rather than throwing', () => {
    const nodes = [makeNode('a'), makeNode('b')];
    const edges: KGEdge[] = [
      makeEdge('a', 'b'),
      makeEdge('a', 'ghost'),
      makeEdge('ghost', 'b'),
    ];
    expect(() => computeForceLayout(nodes, edges)).not.toThrow();
  });
});
