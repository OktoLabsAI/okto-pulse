/**
 * Unit coverage for NodePreviewPanel — Sprint 5 / S5.2 (AC-8).
 *
 * The preview panel is a *lightweight* floating card in the upper-left
 * of the canvas, triggered by single-click selection. Full node details
 * still live in NodeDetailPanel on the right sidebar (opened by double-
 * click). We verify: visibility gating, content rendering, close action,
 * spec-open action visibility, and its aria contract.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { NodePreviewPanel } from '../NodePreviewPanel';
import type { KGNode } from '@/types/knowledge-graph';

const NODE: KGNode = {
  id: 'n1',
  title: 'Adopt TypeScript strict mode',
  content: 'Turning on strict fixes a long list of implicit-any issues.',
  justification: 'Fewer production bugs, better IDE hints.',
  source_confidence: 0.72,
  relevance_score: 0.85,
  node_type: 'Decision',
  source_artifact_ref: 'spec:abcd-1234',
};

describe('NodePreviewPanel — S5.2 / AC-8', () => {
  it('renders nothing when node is null', () => {
    const { container } = render(
      <NodePreviewPanel node={null} onClose={() => {}} />,
    );
    expect(container.querySelector('[data-testid="kg-preview-panel"]')).toBeNull();
  });

  it('renders title, content, justification, and relevance score', () => {
    const { getByText, getByTestId } = render(
      <NodePreviewPanel node={NODE} onClose={() => {}} />,
    );
    expect(getByTestId('kg-preview-panel')).toBeTruthy();
    expect(getByText('Adopt TypeScript strict mode')).toBeTruthy();
    expect(getByText(/Turning on strict/)).toBeTruthy();
    expect(getByText(/Fewer production bugs/)).toBeTruthy();
    expect(getByTestId('relevance-badge')).toBeTruthy();
    expect(getByText('72%')).toBeTruthy();
  });

  it('shows the source artifact reference', () => {
    const { getByText } = render(
      <NodePreviewPanel node={NODE} onClose={() => {}} />,
    );
    expect(getByText('spec:abcd-1234')).toBeTruthy();
  });

  it('fires onClose when the × button is clicked', () => {
    const onClose = vi.fn();
    const { getByTestId } = render(
      <NodePreviewPanel node={NODE} onClose={onClose} />,
    );
    fireEvent.click(getByTestId('kg-preview-close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('shows Open in spec only when source_artifact_ref matches spec:<id> AND onOpenSpec is provided', () => {
    const onOpenSpec = vi.fn();
    const { getByTestId } = render(
      <NodePreviewPanel node={NODE} onClose={() => {}} onOpenSpec={onOpenSpec} />,
    );
    const btn = getByTestId('kg-preview-open-spec');
    fireEvent.click(btn);
    expect(onOpenSpec).toHaveBeenCalledWith('abcd-1234');
  });

  it('hides Open in spec when source_artifact_ref does not match the spec: prefix', () => {
    const { queryByTestId } = render(
      <NodePreviewPanel
        node={{ ...NODE, source_artifact_ref: 'pr:42' }}
        onClose={() => {}}
        onOpenSpec={vi.fn()}
      />,
    );
    expect(queryByTestId('kg-preview-open-spec')).toBeNull();
  });

  it('hides Open in spec when onOpenSpec callback is not provided', () => {
    const { queryByTestId } = render(
      <NodePreviewPanel node={NODE} onClose={() => {}} />,
    );
    expect(queryByTestId('kg-preview-open-spec')).toBeNull();
  });

  it('declares role="dialog" and an aria-label tied to the node title', () => {
    const { getByTestId } = render(
      <NodePreviewPanel node={NODE} onClose={() => {}} />,
    );
    const panel = getByTestId('kg-preview-panel');
    expect(panel.getAttribute('role')).toBe('dialog');
    expect(panel.getAttribute('aria-label')).toBe('Preview of Adopt TypeScript strict mode');
  });
});
