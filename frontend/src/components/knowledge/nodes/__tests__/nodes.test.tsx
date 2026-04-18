/**
 * Vitest snapshot coverage for the 11 KG node components — Spec 8 / S2.7.
 *
 * Each node renders a canonical {@link KGNode} payload and should produce a
 * stable DOM structure so that accidental reshapes of the NodeShell or any
 * of the type-specific wrappers are caught in review.
 *
 * We also exercise the `isSelected` + `hasSelection && !isConnectedToSelected`
 * branches of NodeShell because they drive the AC-3 fade-on-select behaviour.
 */

import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { ReactFlowProvider } from '@xyflow/react';

import type { KGNode } from '@/types/knowledge-graph';
import { nodeTypes } from '../index';
import type { KGNodeData } from '../types';

const BASE_NODE: KGNode = {
  id: 'node-42',
  title: 'Canonical test node',
  content: 'Body used by every snapshot so diffs focus on the wrapper.',
  source_confidence: 0.82,
  validation_status: 'corroborated',
  node_type: 'Decision',
  created_at: '2026-04-15T10:00:00',
};

function makeData(overrides: Partial<KGNodeData> = {}): KGNodeData {
  return {
    kgNode: { ...BASE_NODE, ...(overrides.kgNode ?? {}) },
    isSelected: false,
    isConnectedToSelected: false,
    hasSelection: false,
    ...overrides,
  };
}

function renderNode(type: KGNode['node_type'], data: KGNodeData) {
  const Component = nodeTypes[type];
  if (!Component) {
    throw new Error(`No component registered for node_type=${type}`);
  }
  const props = {
    id: data.kgNode.id,
    type,
    data,
    selected: !!data.isSelected,
    dragging: false,
    draggable: true,
    selectable: true,
    deletable: true,
    isConnectable: true,
    zIndex: 0,
    positionAbsoluteX: 0,
    positionAbsoluteY: 0,
  };
  return render(
    <ReactFlowProvider>
      <Component {...props} />
    </ReactFlowProvider>,
  );
}

const ALL_TYPES: KGNode['node_type'][] = [
  'Decision', 'Criterion', 'Constraint', 'Assumption',
  'Requirement', 'Entity', 'APIContract', 'TestScenario',
  'Bug', 'Learning', 'Alternative',
];

describe('KG node components — base render (AC-2)', () => {
  it.each(ALL_TYPES)('renders %s node with canonical data', (type) => {
    const data = makeData({ kgNode: { ...BASE_NODE, node_type: type } });
    const { container } = renderNode(type, data);
    expect(container.firstChild).toMatchSnapshot();
  });
});

describe('KG node components — selection visuals (AC-3)', () => {
  it('applies selected styling when isSelected=true', () => {
    const data = makeData({ isSelected: true, hasSelection: true });
    const { container } = renderNode('Decision', data);
    expect(container.firstChild).toMatchSnapshot();
  });

  it('fades unconnected nodes when hasSelection && !isConnectedToSelected', () => {
    const data = makeData({
      isSelected: false,
      isConnectedToSelected: false,
      hasSelection: true,
    });
    const { container } = renderNode('Decision', data);
    // The faded variant must be visually distinct from the neutral render.
    expect(container.firstChild).toMatchSnapshot();
  });

  it('keeps neighbours full opacity when isConnectedToSelected=true', () => {
    const data = makeData({
      isSelected: false,
      isConnectedToSelected: true,
      hasSelection: true,
    });
    const { container } = renderNode('Decision', data);
    expect(container.firstChild).toMatchSnapshot();
  });
});

describe('KG node components — nodeTypes map shape', () => {
  it('registers exactly the 11 spec node types', () => {
    expect(Object.keys(nodeTypes).sort()).toEqual([...ALL_TYPES].sort());
  });
});
