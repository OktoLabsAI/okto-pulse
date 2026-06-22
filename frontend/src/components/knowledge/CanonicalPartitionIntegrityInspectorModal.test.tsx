/**
 * Tests for CanonicalPartitionIntegrityInspectorModal (R7 IMP4 / card 434d8dcb).
 *
 * Teeth: the drilldown is READ-ONLY — it renders the partition signals + counts
 * but offers NO agent-style skip / resolve / dismiss / force affordance (R7
 * holds/debt are human-only, enforced in the core, never via a UI button).
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import { CanonicalPartitionIntegrityInspectorModal } from './CanonicalPartitionIntegrityInspectorModal';
import * as api from '@/services/kg-health-api';

const RESP: api.CanonicalPartitionIntegrityResponse = {
  board_id: 'b',
  items: [
    {
      node_id: null,
      node_type: 'Learning',
      artifact_id: 'card:bug:x',
      source_artifact_ref: 'card:bug:x:learning:1',
      reason_code: 'canonical_learning_working_only_bug_evidence_pending',
      graph_layer: 'canonical',
      status: 'cognitive_pending',
      blocking: true,
      canonical_degree: 0,
      working_endpoint_refs: ['bug_w1'],
      operator_action: 'wait for source maturity',
    },
    {
      node_id: 'learn_mix',
      node_type: 'Learning',
      artifact_id: 'card:bug:y',
      source_artifact_ref: 'card:bug:y:learning:2',
      reason_code: 'canonical_learning_mixed_working_edge_deferred',
      graph_layer: 'canonical',
      status: 'mixed_evidence_deferred',
      blocking: false,
      canonical_degree: 1,
      working_endpoint_refs: ['bug_w2'],
      operator_action: 'observe',
    },
  ],
  counts: {
    cognitive_pending: 1,
    canonical_debt: 0,
    mixed_evidence_deferred: 1,
    provenance_only_observed: 0,
  },
  health_issue_code: 'canonical_partition_integrity',
  total: 2,
  limit: 50,
  offset: 0,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CanonicalPartitionIntegrityInspectorModal', () => {
  test('renders signals + counts and is READ-ONLY (no skip/resolve affordance)', async () => {
    vi.spyOn(api, 'getCanonicalPartitionIntegrity').mockResolvedValue(RESP);
    render(<CanonicalPartitionIntegrityInspectorModal boardId="b" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId('cpi-table')).toBeInTheDocument());
    expect(screen.getByTestId('cpi-counts')).toBeInTheDocument();
    expect(screen.getAllByTestId('cpi-row')).toHaveLength(2);
    expect(
      screen.getByText('canonical_learning_working_only_bug_evidence_pending'),
    ).toBeInTheDocument();
    // No agent-style mutation affordance for an R7 hold/debt.
    expect(
      screen.queryByRole('button', {
        name: /skip|resolve|dismiss|force|reprocess/i,
      }),
    ).toBeNull();
  });

  test('empty state when no signals', async () => {
    vi.spyOn(api, 'getCanonicalPartitionIntegrity').mockResolvedValue({
      ...RESP,
      items: [],
      counts: {
        cognitive_pending: 0,
        canonical_debt: 0,
        mixed_evidence_deferred: 0,
        provenance_only_observed: 0,
      },
      total: 0,
    });
    render(<CanonicalPartitionIntegrityInspectorModal boardId="b" onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByTestId('cpi-empty-state')).toBeInTheDocument(),
    );
  });

  test('error state surfaces the failure without breaking', async () => {
    vi.spyOn(api, 'getCanonicalPartitionIntegrity').mockRejectedValue(
      new Error('Network down'),
    );
    render(<CanonicalPartitionIntegrityInspectorModal boardId="b" onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByTestId('cpi-error')).toHaveTextContent(/network down/i),
    );
  });
});
