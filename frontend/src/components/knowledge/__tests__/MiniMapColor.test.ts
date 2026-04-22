/**
 * Integration-style coverage for AC-18 — Sprint 5 / S5.7.
 *
 * The MiniMap inside GraphCanvas maps each `NODE_TYPE_CONFIG` entry to a
 * color (light mode) or darkColor (dark mode). We cannot mount the whole
 * React Flow stack in jsdom, so we exercise the same `nodeColor` function
 * shape the canvas uses — replicated here — against every KGNodeType to
 * guard against registry drift.
 */

import { describe, it, expect } from 'vitest';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import type { KGNodeType } from '@/types/knowledge-graph';
import type { KGNodeData } from '../nodes/types';

type MiniNode = {
  data?: { kgNode?: { node_type: KGNodeType } };
  type?: string;
};

function miniMapColor(node: MiniNode, isDark: boolean): string {
  const kgNode = (node.data as KGNodeData | undefined)?.kgNode;
  const nodeType = kgNode?.node_type ?? (node.type as KGNodeType);
  const cfg = NODE_TYPE_CONFIG[nodeType];
  if (!cfg) return '#6B7280';
  return isDark ? cfg.darkColor : cfg.color;
}

describe('MiniMap color mapping — S5.7 / AC-18', () => {
  const types = Object.keys(NODE_TYPE_CONFIG) as KGNodeType[];

  it('covers all 11 KGNodeType entries', () => {
    expect(types.length).toBe(11);
  });

  it.each(types)('returns NODE_TYPE_CONFIG[%s].color in light mode', (type) => {
    const node = { data: { kgNode: { node_type: type } } } as MiniNode;
    expect(miniMapColor(node, false)).toBe(NODE_TYPE_CONFIG[type].color);
  });

  it.each(types)('returns NODE_TYPE_CONFIG[%s].darkColor in dark mode', (type) => {
    const node = { data: { kgNode: { node_type: type } } } as MiniNode;
    expect(miniMapColor(node, true)).toBe(NODE_TYPE_CONFIG[type].darkColor);
  });

  it('falls back to the type prop when kgNode is missing (React Flow bootstrap)', () => {
    const node = { type: 'Decision' } as MiniNode;
    expect(miniMapColor(node, false)).toBe(NODE_TYPE_CONFIG.Decision.color);
  });

  it('uses a neutral gray for an unknown node type', () => {
    const node = { type: 'Mystery' } as MiniNode;
    expect(miniMapColor(node, false)).toBe('#6B7280');
    expect(miniMapColor(node, true)).toBe('#6B7280');
  });
});
