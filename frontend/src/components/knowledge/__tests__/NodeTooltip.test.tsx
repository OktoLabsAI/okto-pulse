/**
 * Unit coverage for NodeTooltip — Sprint 5 / S5.1 (AC-7).
 *
 * AC-7 asks for the tooltip to appear in <=100ms from onMouseEnter. Since
 * the component renders synchronously on a prop change, we only assert
 * behavioural correctness here (presence + content + truncation) — the
 * wall-clock appearance latency is enforced by the Playwright spec
 * `graph-tooltip.spec.ts` (S5.6).
 */

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { NodeTooltip } from '../NodeTooltip';
import type { KGNode } from '@/types/knowledge-graph';

const BASE_NODE: KGNode = {
  id: 'n1',
  title: 'Pick PostgreSQL over Mongo',
  content: 'We chose Postgres because relational queries dominate our access pattern.',
  source_confidence: 0.87,
  relevance_score: 0.85,
  node_type: 'Decision',
  source_artifact_ref: 'spec:dc9075a9',
  created_at: '2026-04-16T00:00:00',
};

describe('NodeTooltip — S5.1 / AC-7', () => {
  it('renders nothing when node is null', () => {
    const { container } = render(<NodeTooltip node={null} />);
    expect(container.querySelector('[data-testid="kg-node-tooltip"]')).toBeNull();
  });

  it('shows title, type, confidence % and source ref', () => {
    const { getByTestId, getByText } = render(<NodeTooltip node={BASE_NODE} />);
    const tooltip = getByTestId('kg-node-tooltip');
    expect(tooltip).toBeTruthy();
    expect(getByText('Pick PostgreSQL over Mongo')).toBeTruthy();
    expect(getByText('Decision')).toBeTruthy();
    expect(getByText('conf 87%')).toBeTruthy();
    expect(getByText('spec:dc9075a9')).toBeTruthy();
  });

  it('truncates content over 200 chars with ellipsis', () => {
    const long = 'A'.repeat(400);
    const { getByTestId } = render(
      <NodeTooltip node={{ ...BASE_NODE, content: long }} />,
    );
    const content = getByTestId('kg-node-tooltip-content').textContent ?? '';
    // 200-char cut + ellipsis → 201 characters total
    expect(content.length).toBe(201);
    expect(content.endsWith('…')).toBe(true);
  });

  it('omits content block when node has no content', () => {
    const { queryByTestId } = render(
      <NodeTooltip node={{ ...BASE_NODE, content: undefined }} />,
    );
    expect(queryByTestId('kg-node-tooltip-content')).toBeNull();
  });

  it('has role="tooltip" and an aria-label derived from the title', () => {
    const { getByTestId } = render(<NodeTooltip node={BASE_NODE} />);
    const tooltip = getByTestId('kg-node-tooltip');
    expect(tooltip.getAttribute('role')).toBe('tooltip');
    expect(tooltip.getAttribute('aria-label')).toBe(
      'Tooltip for Pick PostgreSQL over Mongo',
    );
  });
});
