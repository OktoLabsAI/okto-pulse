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
  nextCursor: string | null = null,
): GuidelineImpactPreviewResponse {
  return {
    preview_id: 'preview-1',
    preview_digest: 'b'.repeat(64),
    items_page: {
      items: [{
        impact_item_id: 'impact-1',
        item_kind: 'binding',
        entity_type: 'board',
        entity_id: 'board-1',
        related_id: 'binding-1',
        entity_version: 4,
        details_digest: 'c'.repeat(64),
      }],
      next_cursor: nextCursor,
    },
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
    policyApiMock.adoptGuidelineRevision.mockResolvedValue({
      binding_id: 'binding-1',
      binding_revision: 5,
      configuration_digest: 'e'.repeat(64),
      replayed: false,
    });
  });

  it('previews the exact semantic configuration without legacy aliases', async () => {
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
          target_revision_id: 'revision-2',
          expected_binding_head_revision: 4,
          enforcement: 'blocking',
          minimum_confidence: 82,
          metric_threshold_overrides: {
            evidence_strength: 76,
          },
        },
        expect.any(AbortSignal),
      );
    });
    const request = policyApiMock.previewGuidelineImpact.mock.calls[0][2];
    expect(request).not.toHaveProperty('proposed_default_enforcement');
    expect(request).not.toHaveProperty('proposed_priority');
    expect(request).not.toHaveProperty('to_revision_id');
    expect(request).not.toHaveProperty('idempotency_key');
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
          preview_id: 'preview-1',
          preview_digest: 'b'.repeat(64),
          expected_binding_head_revision: 4,
          idempotency_key: expect.stringMatching(/^guideline-adoption-/),
        },
        expect.any(AbortSignal),
      );
    });
    expect(onAdopted).toHaveBeenCalledWith({
      binding_id: 'binding-1',
      binding_revision: 5,
      configuration_digest: 'e'.repeat(64),
      replayed: false,
    });
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

  it('does not invent continuation pagination when preview exposes a cursor', async () => {
    policyApiMock.previewGuidelineImpact.mockResolvedValue(
      preview('opaque-next'),
    );
    renderDialog();
    await screen.findByText('Evidence strength');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(await screen.findByTestId('guideline-impact-more-items'))
      .toHaveTextContent(/Adoption stays disabled/i);
    expect(policyApiMock).not.toHaveProperty('listGuidelineImpactItems');
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
  });
});
