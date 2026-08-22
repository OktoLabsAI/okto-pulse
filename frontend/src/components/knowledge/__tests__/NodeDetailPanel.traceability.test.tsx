import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { KGNode } from '@/types/knowledge-graph';
import { NodeDetailPanel } from '../NodeDetailPanel';

const authority = vi.hoisted(() => ({ canReadProjection: true }));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    has: () => false,
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
  }),
}));

vi.mock('@/components/code-traceability', () => ({
  useCodeTraceabilityAuthority: () => ({
    canReadProjection: authority.canReadProjection,
    canRevokeReceipt: false,
    canCreateTarget: false,
    canAcknowledgeOverlap: false,
    isLoading: false,
    error: null,
  }),
}));

const TARGET_NODE: KGNode = {
  id: 'target-node-1',
  title: 'Authorize payment target',
  content: 'Semantic implementation intent.',
  source_confidence: 0.95,
  relevance_score: 0.8,
  node_type: 'Entity',
  kind_of: 'implementation_target',
  investigation_receipt_id: 'receipt-103',
  source_ref: 'source:opaque-1',
  attestor_actor_id: 'agent:trace',
  declared_revision: 'abc123',
  workspace_state_id: 'workspace-103',
  code_path: 'src/payments/service.ts',
  symbol_qualified_name: 'PaymentsService.authorize',
  symbol_kind: 'method',
  selector_kind: 'symbol',
  selector_fingerprint: 'a'.repeat(64),
  resolution_state: 'resolved',
};

afterEach(() => {
  authority.canReadProjection = true;
  cleanup();
});

describe('NodeDetailPanel Code Traceability inspector', () => {
  it('shows the logical subtype and accepted metadata with explicit authority', () => {
    render(
      <NodeDetailPanel
        node={TARGET_NODE}
        boardId="board-1"
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/Implementation Target/)).toBeInTheDocument();
    expect(screen.getByText('implementation_target')).toBeInTheDocument();
    expect(screen.getByTestId('kg-code-traceability-metadata')).toHaveTextContent(
      'PaymentsService.authorize',
    );
    expect(screen.getByTestId('kg-code-traceability-metadata')).toHaveTextContent(
      'Agent-attested traceability metadata',
    );
  });

  it('closes without rendering any node content when traceability leaves are denied', async () => {
    authority.canReadProjection = false;
    const onClose = vi.fn();
    const { container } = render(
      <NodeDetailPanel
        node={TARGET_NODE}
        boardId="board-1"
        onClose={onClose}
      />,
    );

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText('Authorize payment target')).not.toBeInTheDocument();
    expect(screen.queryByText('implementation_target')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kg-code-traceability-metadata')).not.toBeInTheDocument();
  });
});
