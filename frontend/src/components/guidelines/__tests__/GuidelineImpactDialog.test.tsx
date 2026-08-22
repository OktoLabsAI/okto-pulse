import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { PolicyGovernanceApiError } from '@/services/policy-governance-api';
import type {
  GuidelineAdoptionResponse,
  GuidelineImpactPreviewResponse,
  GuidelineMetric,
} from '@/types/policy-governance';

const policyApiMock = vi.hoisted(() => ({
  getGuidelineRevision: vi.fn(),
  previewGuidelineImpact: vi.fn(),
  adoptGuidelineRevision: vi.fn(),
}));
const permissionState = vi.hoisted(() => ({
  isLoading: false,
  error: null as Error | null,
  ownerReviewRequired: false,
  allowed: new Set<string>(),
}));

vi.mock('@/services/policy-governance-api', async () => {
  const actual = await vi.importActual<
    typeof import('@/services/policy-governance-api')
  >('@/services/policy-governance-api');
  return {
    ...actual,
    usePolicyGovernanceApi: () => policyApiMock,
  };
});
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Custom',
    isLoading: permissionState.isLoading,
    error: permissionState.error,
    ownerReviewRequired: permissionState.ownerReviewRequired,
    has: (flag: string) => permissionState.allowed.has(flag),
  }),
}));

import { GuidelineImpactDialog } from '../GuidelineImpactDialog';

const metric: GuidelineMetric = {
  metric_id: 'metric-1',
  code: 'evidence_strength',
  title: 'Evidence strength',
  description: 'How strongly evidence supports the proposal.',
  evaluation_rubric: '0 has no evidence; 100 is independently traceable.',
  target_entity_types: ['spec'],
  direction: 'minimum',
  default_threshold: 70,
};

function authority(metrics: GuidelineMetric[] = [metric]) {
  return {
    guideline: {
      guideline_id: 'guideline-1',
      owner_id: 'owner-1',
      scope: 'global',
      created_at: '2026-07-30T10:00:00Z',
      context_scope: 'all',
    },
    revision: {
      revision_id: 'revision-2',
      guideline_id: 'guideline-1',
      revision_number: 2,
      semantic_version: '2.0.0',
      title: 'Delivery quality',
      content: 'Attach traceable evidence.',
      revision_digest: 'a'.repeat(64),
      metrics,
      created_by: 'owner-1',
      created_at: '2026-07-30T11:00:00Z',
      parent_revision_id: 'revision-1',
      tags: ['delivery'],
    },
    head: {
      guideline_id: 'guideline-1',
      revision_id: 'revision-2',
      revision_number: 2,
      semantic_version: '2.0.0',
      head_revision: 2,
      updated_at: '2026-07-30T11:00:00Z',
    },
  };
}

function preview(
  {
    priority = 2,
    enforcement = 'advisory',
    minimumConfidence = 70,
    overrides = {},
  }: {
    priority?: number;
    enforcement?: 'advisory' | 'blocking';
    minimumConfidence?: number;
    overrides?: Record<string, number>;
  } = {},
): GuidelineImpactPreviewResponse {
  return {
    receipt: {
      impact_receipt_id: 'preview-1',
      board_id: 'board-1',
      guideline_id: 'guideline-1',
      binding_id: 'binding-1',
      to_revision_id: 'revision-2',
      to_revision_number: 2,
      to_semantic_version: '2.0.0',
      to_revision_digest: 'a'.repeat(64),
      expected_head_revision: 2,
      expected_binding_revision: 4,
      expected_binding_state: 'active',
      binding_digest: 'e'.repeat(64),
      binding_head_digest_before: '1'.repeat(64),
      binding_head_digest_after: '2'.repeat(64),
      policy_set_digest_before: '3'.repeat(64),
      policy_set_digest_after: '4'.repeat(64),
      artifact_snapshot_digest: '5'.repeat(64),
      waiver_snapshot_digest: '6'.repeat(64),
      proposed_priority: priority,
      proposed_enforcement: enforcement,
      proposed_minimum_confidence: minimumConfidence,
      proposed_metric_threshold_overrides: overrides,
      affected_entity_types: ['spec'],
      items: [{
        impact_item_id: 'impact-1',
        item_kind: 'binding',
        entity_type: 'board',
        entity_id: 'board-1',
        related_id: 'binding-1',
        entity_version: 4,
        details_digest: 'c'.repeat(64),
      }],
      added_metric_ids: [],
      changed_metric_ids: [],
      removed_metric_ids: [],
      requested_by: 'owner-1',
      created_at: '2026-07-30T12:00:00Z',
      impact_digest: 'b'.repeat(64),
      from_revision_id: 'revision-1',
      from_semantic_version: '1.0.0',
      from_revision_digest: 'd'.repeat(64),
      requires_explicit_adoption: true,
    },
  };
}

function adoption(
  impact: GuidelineImpactPreviewResponse = preview(),
): GuidelineAdoptionResponse {
  return {
    binding: {
      binding_id: 'binding-1',
      board_id: 'board-1',
      guideline_id: 'guideline-1',
      revision_id: 'revision-2',
      semantic_version: '2.0.0',
      revision_digest: 'a'.repeat(64),
      priority: impact.receipt.proposed_priority,
      binding_revision: 5,
      adopted_by: 'owner-1',
      adopted_at: '2026-07-30T12:01:00Z',
      enforcement: impact.receipt.proposed_enforcement,
      minimum_confidence: impact.receipt.proposed_minimum_confidence,
      metric_threshold_overrides:
        impact.receipt.proposed_metric_threshold_overrides,
      configuration_digest: '7'.repeat(64),
      state: 'active',
      source_kind: 'native',
    },
    receipt: impact.receipt,
  };
}

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

function renderDialog({
  metrics = [metric],
  adopted = true,
  onAddSemanticMetrics = vi.fn(),
  onAdopted = vi.fn<(response: GuidelineAdoptionResponse) => void>(),
}: {
  metrics?: GuidelineMetric[];
  adopted?: boolean;
  onAddSemanticMetrics?: () => void;
  onAdopted?: (response: GuidelineAdoptionResponse) => void;
} = {}) {
  policyApiMock.getGuidelineRevision.mockResolvedValue(authority(metrics));
  return {
    ...render(
      <GuidelineImpactDialog
        boardId="board-1"
        guidelineId="guideline-1"
        guidelineTitle="Delivery quality"
        targetRevisionId="revision-2"
        targetSemanticVersion="2.0.0"
        proposedPriority={2}
        adoptedBinding={adopted
          ? {
              bindingId: 'binding-1',
              bindingRevision: 4,
              bindingState: 'active',
              revisionId: 'revision-1',
              semanticVersion: '1.0.0',
              revisionDigest: 'd'.repeat(64),
            }
          : undefined}
        initialEnforcement="advisory"
        initialMinimumConfidence={70}
        initialMetricThresholdOverrides={{}}
        onAddSemanticMetrics={onAddSemanticMetrics}
        onClose={vi.fn()}
        onAdopted={onAdopted}
      />,
    ),
    onAddSemanticMetrics,
    onAdopted,
  };
}

describe('GuidelineImpactDialog semantic board configuration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.isLoading = false;
    permissionState.error = null;
    permissionState.ownerReviewRequired = false;
    grant(
      'guidelines.revisions.read',
      'guidelines.impact.preview',
      'guidelines.adoption.manage',
    );
    policyApiMock.previewGuidelineImpact.mockResolvedValue(preview());
    policyApiMock.adoptGuidelineRevision.mockResolvedValue(adoption());
  });

  it('previews the exact semantic configuration without legacy aliases', async () => {
    policyApiMock.previewGuidelineImpact.mockResolvedValue(preview({
      enforcement: 'blocking',
      minimumConfidence: 82,
      overrides: { evidence_strength: 76 },
    }));
    renderDialog();
    expect(await screen.findByText('Evidence strength')).toBeInTheDocument();
    expect(screen.getByText(/Confidence is system-owned/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Blocking/i }));
    fireEvent.change(
      screen.getByLabelText('Minimum assessment confidence value'),
      { target: { value: '82' } },
    );
    fireEvent.click(
      screen.getByRole('switch', { name: 'Guideline default' }),
    );
    fireEvent.change(
      screen.getByLabelText('Evidence strength threshold value'),
      { target: { value: '76' } },
    );
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    await waitFor(() => {
      expect(policyApiMock.previewGuidelineImpact).toHaveBeenCalledWith(
        'board-1',
        'guideline-1',
        {
          proposed_priority: 2,
          proposed_enforcement: 'blocking',
          proposed_minimum_confidence: 82,
          proposed_metric_threshold_overrides: {
            evidence_strength: 76,
          },
          idempotency_key: expect.stringMatching(
            /^guideline-impact-preview-/,
          ),
          to_revision_id: 'revision-2',
        },
        expect.any(AbortSignal),
      );
    });
    const request = policyApiMock.previewGuidelineImpact.mock.calls[0][2];
    expect(request).not.toHaveProperty('target_revision_id');
    expect(request).not.toHaveProperty('expected_binding_head_revision');
    expect(request).not.toHaveProperty('metric_threshold_overrides');
    expect(await screen.findByText('Impact preview is ready.'))
      .toBeInTheDocument();
  });

  it('adopts using the preview identity and binding-head fence', async () => {
    const { onAdopted } = renderDialog();
    await screen.findByText('Evidence strength');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByText('Impact preview is ready.');

    fireEvent.click(screen.getByTestId('guideline-impact-adopt'));

    await waitFor(() => {
      expect(policyApiMock.adoptGuidelineRevision).toHaveBeenCalledWith(
        'board-1',
        'guideline-1',
        {
          impact_receipt_id: 'preview-1',
          impact_digest: 'b'.repeat(64),
          idempotency_key: expect.stringMatching(/^guideline-adoption-/),
        },
        expect.any(AbortSignal),
      );
    });
    expect(onAdopted).toHaveBeenCalledWith(adoption());
  });

  it('invalidates a preview whenever a board setting changes', async () => {
    renderDialog();
    await screen.findByText('Evidence strength');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByText('Impact preview is ready.');

    fireEvent.change(
      screen.getByLabelText('Minimum assessment confidence value'),
      { target: { value: '80' } },
    );

    expect(screen.queryByText('Impact preview is ready.'))
      .not.toBeInTheDocument();
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
  });

  it('keeps context-only adoption understandable and links to metric authoring', async () => {
    const { onAddSemanticMetrics } = renderDialog({ metrics: [] });
    expect(await screen.findByText(/Context only · no scored metric/i))
      .toBeInTheDocument();

    fireEvent.click(screen.getByTestId('guideline-impact-add-metrics'));
    expect(onAddSemanticMetrics).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole('button', { name: /Blocking/i }),
    ).toBeDisabled();
  });

  it('fails closed on a wrapped or malformed preview response', async () => {
    policyApiMock.previewGuidelineImpact.mockResolvedValue({
      preview: preview(),
    });
    renderDialog();
    await screen.findByText('Evidence strength');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /mismatched payload/i,
    );
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
  });

  it('surfaces stale binding conflicts and requires a fresh preview', async () => {
    policyApiMock.previewGuidelineImpact.mockRejectedValue(
      new PolicyGovernanceApiError({
        message: 'Preview is stale',
        status: 409,
        kind: 'conflict',
        code: 'guideline_impact_stale',
        nextAction: 'retry',
        details: { reason_code: 'guideline_impact_stale' },
      }),
    );
    renderDialog();
    await screen.findByText('Evidence strength');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(await screen.findByTestId('guideline-impact-conflict'))
      .toHaveTextContent(/changed while this preview was prepared/i);
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
  });

  it('uses the immutable affected-item set sealed by the receipt', async () => {
    renderDialog();
    await screen.findByText('Evidence strength');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(await screen.findByTestId('guideline-impact-item-impact-1'))
      .toHaveTextContent(/Board configuration/i);
    expect(policyApiMock).not.toHaveProperty('listGuidelineImpactItems');
    expect(screen.getByTestId('guideline-impact-adopt')).toBeEnabled();
  });
});
