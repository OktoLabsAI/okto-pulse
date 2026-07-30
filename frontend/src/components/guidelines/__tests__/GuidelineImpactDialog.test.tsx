import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CONTEXTUAL_HELP_EVENT } from '@/components/help';
import type {
  GuidelineAdoptionResponse,
  GuidelineImpactPageItem,
  GuidelineImpactReceipt,
  GuidelineRule,
  GuidelineRevisionAuthorityResponse,
} from '@/types/policy-governance';

const policyApiMock = vi.hoisted(() => ({
  getGuidelineRevision: vi.fn(),
  previewGuidelineImpact: vi.fn(),
  listGuidelineImpactItems: vi.fn(),
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

import { PolicyGovernanceApiError } from '@/services/policy-governance-api';
import { GuidelineImpactDialog } from '../GuidelineImpactDialog';

const digest = (character: string) => character.repeat(64);

const blockingRule: GuidelineRule = {
  rule_id: 'rule-1',
  code: 'require_evidence',
  title: 'Require evidence',
  description: 'Evidence must be attached.',
  target_entity_types: ['spec'],
  predicates: [{
    predicate_code: 'field_present',
    parameters: [['field', 'evidence']],
  }],
  enforcement: 'blocking' as const,
  operator: 'all' as const,
  waivable: true,
  policy_class: 'quality',
};

function authority(): GuidelineRevisionAuthorityResponse {
  return {
    guideline: {
      guideline_id: 'guideline-1',
      owner_id: 'owner-1',
      scope: 'global',
      created_at: '2026-07-29T00:00:00Z',
      context_scope: 'all',
    },
    revision: {
      revision_id: 'revision-2',
      guideline_id: 'guideline-1',
      revision_number: 2,
      semantic_version: '2.0.0',
      title: 'Evidence policy',
      content: 'Context for every entity.',
      content_digest: digest('a'),
      rules: [blockingRule],
      created_by: 'owner-1',
      created_at: '2026-07-29T00:00:00Z',
      parent_revision_id: 'revision-1',
      tags: ['quality'],
    },
    head: {
      guideline_id: 'guideline-1',
      revision_id: 'revision-2',
      revision_number: 2,
      semantic_version: '2.0.0',
      head_revision: 2,
      updated_at: '2026-07-29T00:00:00Z',
    },
  };
}

function receipt(
  receiptId = 'impact-1',
  priority = 4,
): GuidelineImpactReceipt {
  return {
    impact_receipt_id: receiptId,
    board_id: 'board-1',
    guideline_id: 'guideline-1',
    binding_id: 'binding-1',
    to_revision_id: 'revision-2',
    to_revision_number: 2,
    to_semantic_version: '2.0.0',
    to_revision_digest: digest('a'),
    expected_head_revision: 2,
    expected_binding_revision: 1,
    expected_binding_state: 'active',
    binding_digest: digest('b'),
    binding_head_digest_before: digest('c'),
    binding_head_digest_after: digest('d'),
    policy_set_digest_before: digest('e'),
    policy_set_digest_after: digest('f'),
    artifact_snapshot_digest: digest('1'),
    waiver_snapshot_digest: digest('2'),
    proposed_priority: priority,
    proposed_default_enforcement: 'blocking',
    affected_entity_types: ['spec', 'card'],
    items: [
      {
        impact_item_id: 'binding-item',
        item_kind: 'binding',
        entity_type: 'board',
        entity_id: 'board-1',
        related_id: 'binding-1',
        entity_version: 1,
        details_digest: digest('3'),
      },
      {
        impact_item_id: 'target-item',
        item_kind: 'target',
        entity_type: 'spec',
        entity_id: 'spec-1',
        related_id: 'rule-1',
        entity_version: 7,
        details_digest: digest('4'),
      },
      {
        impact_item_id: 'artifact-item',
        item_kind: 'artifact',
        entity_type: 'card',
        entity_id: 'card-1',
        related_id: 'spec-1',
        entity_version: 3,
        details_digest: digest('5'),
      },
      {
        impact_item_id: 'waiver-item',
        item_kind: 'waiver',
        entity_type: 'spec',
        entity_id: 'spec-1',
        related_id: 'waiver-1',
        entity_version: 7,
        details_digest: digest('6'),
      },
    ],
    added_rule_ids: ['rule-2'],
    changed_rule_ids: ['rule-1'],
    removed_rule_ids: ['rule-old'],
    requested_by: 'operator-1',
    created_at: '2026-07-29T00:00:00Z',
    impact_digest: digest('7'),
    from_revision_id: 'revision-1',
    from_semantic_version: '1.0.0',
    from_revision_digest: digest('8'),
    requires_explicit_adoption: true,
  };
}

function pageItem(
  id: string,
  kind: 'target' | 'waiver' = 'target',
): GuidelineImpactPageItem {
  return {
    impact_item_id: id,
    item_kind: kind,
    entity_type: 'spec',
    entity_id: `spec-${id}`,
    related_id: kind === 'waiver' ? `waiver-${id}` : `rule-${id}`,
    entity_version: 1,
    details_digest: digest(kind === 'waiver' ? '9' : '0'),
  };
}

function adoption(
  expectedReceipt: GuidelineImpactReceipt,
): GuidelineAdoptionResponse {
  return {
    binding: {
      binding_id: 'binding-1',
      board_id: 'board-1',
      guideline_id: 'guideline-1',
      revision_id: 'revision-2',
      semantic_version: '2.0.0',
      revision_digest: digest('a'),
      priority: expectedReceipt.proposed_priority,
      binding_revision: 2,
      adopted_by: 'operator-1',
      adopted_at: '2026-07-29T00:00:01Z',
      default_enforcement: 'blocking',
      state: 'active',
      source_kind: 'native',
    },
    receipt: expectedReceipt,
  };
}

const renderDialog = (
  overrides: Partial<React.ComponentProps<typeof GuidelineImpactDialog>> = {},
) => {
  const onClose = vi.fn();
  const onAdopted = vi.fn();
  render(
    <GuidelineImpactDialog
      boardId="board-1"
      guidelineId="guideline-1"
      guidelineTitle="Evidence policy"
      targetRevisionId="revision-2"
      targetSemanticVersion="2.0.0"
      adoptedBinding={{
        bindingId: 'binding-1',
        bindingRevision: 1,
        bindingState: 'active',
        revisionId: 'revision-1',
        semanticVersion: '1.0.0',
        revisionDigest: digest('8'),
      }}
      initialPriority={4}
      initialEnforcement="blocking"
      onClose={onClose}
      onAdopted={onAdopted}
      {...overrides}
    />,
  );
  return { onClose, onAdopted };
};

describe('GuidelineImpactDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.isLoading = false;
    permissionState.error = null;
    permissionState.ownerReviewRequired = false;
    permissionState.allowed = new Set([
      'guidelines.revisions.read',
      'guidelines.impact.preview',
      'guidelines.adoption.manage',
    ]);
    policyApiMock.getGuidelineRevision.mockResolvedValue(authority());
    policyApiMock.previewGuidelineImpact.mockResolvedValue({
      receipt: receipt(),
    });
    policyApiMock.listGuidelineImpactItems.mockResolvedValue({
      items: [pageItem('one'), pageItem('two', 'waiver')],
      limit: 50,
      has_more: false,
      next_cursor: null,
    });
    policyApiMock.adoptGuidelineRevision.mockResolvedValue(
      adoption(receipt()),
    );
  });

  it('moves from zero to exactly one enabled adoption control after a valid persisted preview', async () => {
    const { onClose, onAdopted } = renderDialog();

    const adoptButton = screen.getByTestId('guideline-impact-adopt');
    expect(adoptButton).toBeDisabled();
    expect(screen.getAllByTestId('guideline-impact-adopt')).toHaveLength(1);
    expect(await screen.findByText('All entities')).toBeInTheDocument();
    expect(screen.getByTestId(
      'guideline-impact-contains-blocking',
    )).toBeInTheDocument();
    expect(screen.getByTestId('guideline-impact-help'))
      .toHaveTextContent('Board guideline guide');
    expect(
      screen.queryByTestId('guideline-impact-enforcement'),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(
      await screen.findByTestId('guideline-impact-current-receipt'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('guideline-impact-waiver-warning'))
      .toHaveTextContent('1 governed waiver');
    expect(adoptButton).toBeEnabled();
    expect(screen.getAllByTestId('guideline-impact-adopt')).toHaveLength(1);
    expect(policyApiMock.previewGuidelineImpact).toHaveBeenCalledWith(
      'board-1',
      'guideline-1',
      expect.objectContaining({
        proposed_priority: 4,
        proposed_default_enforcement: 'blocking',
        to_revision_id: 'revision-2',
        idempotency_key: expect.stringMatching(/^guideline-impact-preview-/),
      }),
      expect.any(AbortSignal),
    );
    await waitFor(() => {
      expect(policyApiMock.listGuidelineImpactItems).toHaveBeenCalledWith(
        'board-1',
        'guideline-1',
        'impact-1',
        expect.objectContaining({
          limit: 50,
          projection: 'detail',
          cursor: undefined,
          signal: expect.any(AbortSignal),
        }),
      );
    });

    fireEvent.click(adoptButton);
    fireEvent.click(adoptButton);

    await waitFor(() =>
      expect(policyApiMock.adoptGuidelineRevision).toHaveBeenCalledTimes(1),
    );
    expect(policyApiMock.adoptGuidelineRevision).toHaveBeenCalledWith(
      'board-1',
      'guideline-1',
      expect.objectContaining({
        impact_receipt_id: 'impact-1',
        impact_digest: digest('7'),
        idempotency_key: expect.stringMatching(/^guideline-adoption-/),
      }),
      expect.any(AbortSignal),
    );
    await waitFor(() => expect(onAdopted).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('opens canonical policy Help from the impact dialog', () => {
    const helpListener = vi.fn();
    window.addEventListener(CONTEXTUAL_HELP_EVENT, helpListener, {
      once: true,
    });
    renderDialog();

    fireEvent.click(screen.getByTestId('guideline-impact-help'));

    expect(helpListener).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { sectionId: 'policy-governance' },
      }),
    );
  });

  it('allows preview-only inspection but never enables adoption', async () => {
    permissionState.allowed.delete('guidelines.adoption.manage');
    renderDialog();
    await screen.findByText('All entities');

    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(
      await screen.findByTestId('guideline-impact-current-receipt'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
    expect(screen.getByText(/Preview access only/i)).toBeInTheDocument();
    expect(policyApiMock.adoptGuidelineRevision).not.toHaveBeenCalled();
  });

  it('fails closed for unavailable permissions without reading policy data', async () => {
    permissionState.error = new Error('permission service offline');
    renderDialog();

    expect(
      await screen.findByTestId('guideline-impact-authority-message'),
    ).toHaveTextContent('permissions are unavailable');
    expect(screen.getByTestId('guideline-impact-preview')).toBeDisabled();
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
    expect(policyApiMock.getGuidelineRevision).not.toHaveBeenCalled();
    expect(policyApiMock.previewGuidelineImpact).not.toHaveBeenCalled();
    expect(screen.getByTestId('guideline-impact-help'))
      .toHaveTextContent('Board guideline guide');
  });

  it('rejects malformed or mismatched receipts and keeps adoption disabled', async () => {
    policyApiMock.previewGuidelineImpact.mockResolvedValue({
      receipt: {
        ...receipt(),
        board_id: 'board-other',
      },
    });
    renderDialog();
    await screen.findByText('All entities');

    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'impact preview could not be verified',
    );
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
    expect(policyApiMock.listGuidelineImpactItems).not.toHaveBeenCalled();
  });

  it('fails closed when the guideline head advances after authority load', async () => {
    policyApiMock.previewGuidelineImpact.mockResolvedValue({
      receipt: {
        ...receipt(),
        expected_head_revision: 3,
      },
    });
    renderDialog();
    await screen.findByText('All entities');

    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'newer guideline revision is now available',
    );
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
  });

  it('invalidates a receipt immediately when proposed inputs change', async () => {
    renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    expect(
      await screen.findByTestId('guideline-impact-current-receipt'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('guideline-impact-adopt')).toBeEnabled();

    fireEvent.change(screen.getByTestId('guideline-impact-priority'), {
      target: { value: '5' },
    });

    await waitFor(() =>
      expect(screen.queryByTestId(
        'guideline-impact-current-receipt',
      )).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
  });

  it('does not preview an unchanged context-only binding and opens the rules editor CTA', async () => {
    const contextAuthority = authority();
    contextAuthority.revision.rules = [];
    policyApiMock.getGuidelineRevision.mockResolvedValue(contextAuthority);
    const onAddExecutableRules = vi.fn();
    renderDialog({
      adoptedBinding: {
        bindingId: 'binding-1',
        bindingRevision: 1,
        bindingState: 'active',
        revisionId: 'revision-2',
        semanticVersion: '2.0.0',
        revisionDigest: digest('a'),
      },
      onAddExecutableRules,
      autoPreview: true,
    });

    expect(
      await screen.findByTestId('guideline-impact-no-changes'),
    ).toHaveTextContent('provides context only');
    expect(screen.getByTestId('guideline-impact-preview')).toBeDisabled();
    expect(policyApiMock.previewGuidelineImpact).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('guideline-impact-add-rules'));
    expect(onAddExecutableRules).toHaveBeenCalledTimes(1);
  });

  it('auto-previews a new context-only binding with reserved advisory metadata', async () => {
    const contextAuthority = authority();
    contextAuthority.revision.rules = [];
    policyApiMock.getGuidelineRevision.mockResolvedValue(contextAuthority);
    const unboundReceipt: GuidelineImpactReceipt = {
      ...receipt(),
      expected_binding_revision: null,
      expected_binding_state: null,
      proposed_default_enforcement: 'advisory',
      affected_entity_types: [],
      items: [receipt().items[0]],
      added_rule_ids: [],
      changed_rule_ids: [],
      removed_rule_ids: [],
      from_revision_id: null,
      from_semantic_version: null,
      from_revision_digest: null,
    };
    policyApiMock.previewGuidelineImpact.mockResolvedValue({
      receipt: unboundReceipt,
    });
    renderDialog({
      adoptedBinding: undefined,
      initialEnforcement: 'blocking',
      autoPreview: true,
    });

    await waitFor(() => {
      expect(policyApiMock.previewGuidelineImpact).toHaveBeenCalledWith(
        'board-1',
        'guideline-1',
        expect.objectContaining({
          proposed_default_enforcement: 'advisory',
        }),
        expect.any(AbortSignal),
      );
    });
    expect(
      await screen.findByTestId('guideline-impact-current-receipt'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('guideline-impact-adopt'))
      .toHaveTextContent('Add to board');
  });

  it('keeps receipt identifiers hidden until technical details are expanded', async () => {
    renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByTestId('guideline-impact-current-receipt');

    expect(screen.queryByText(/Preview ID impact-1/)).not.toBeInTheDocument();
    expect(screen.queryByText(`Digest ${digest('7')}`)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('guideline-impact-technical-toggle'));
    expect(screen.getByText(/Preview ID impact-1/)).toBeInTheDocument();
    expect(screen.getByText(`Digest ${digest('7')}`)).toBeInTheDocument();
  });

  it('reuses a preview idempotency key for retry and rotates it for explicit reload after conflict', async () => {
    policyApiMock.previewGuidelineImpact
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockRejectedValueOnce(new PolicyGovernanceApiError({
        message: 'conflict',
        status: 409,
        kind: 'conflict',
        code: 'conflict',
      }))
      .mockResolvedValueOnce({ receipt: receipt('impact-2') });
    renderDialog();
    await screen.findByText('All entities');

    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'temporary failure',
    );
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    expect(
      await screen.findByTestId('guideline-impact-conflict'),
    ).toBeInTheDocument();

    const firstKey = policyApiMock.previewGuidelineImpact.mock.calls[0][2]
      .idempotency_key;
    const retryKey = policyApiMock.previewGuidelineImpact.mock.calls[1][2]
      .idempotency_key;
    expect(retryKey).toBe(firstKey);

    fireEvent.click(screen.getByTestId('guideline-impact-reload'));
    expect(
      await screen.findByTestId('guideline-impact-current-receipt'),
    ).toBeInTheDocument();
    const reloadKey = policyApiMock.previewGuidelineImpact.mock.calls[2][2]
      .idempotency_key;
    expect(reloadKey).not.toBe(firstKey);
  });

  it('rotates the preview key when the user explicitly refreshes a current receipt', async () => {
    policyApiMock.previewGuidelineImpact
      .mockResolvedValueOnce({ receipt: receipt('impact-1') })
      .mockResolvedValueOnce({ receipt: receipt('impact-2') });
    renderDialog();
    await screen.findByText('All entities');

    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByTestId('guideline-impact-current-receipt');
    const initialKey = policyApiMock.previewGuidelineImpact.mock.calls[0][2]
      .idempotency_key;

    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    await waitFor(() =>
      expect(policyApiMock.previewGuidelineImpact).toHaveBeenCalledTimes(2),
    );
    fireEvent.click(screen.getByTestId('guideline-impact-technical-toggle'));
    expect(await screen.findByText(/Preview ID impact-2/)).toBeInTheDocument();
    const refreshKey = policyApiMock.previewGuidelineImpact.mock.calls[1][2]
      .idempotency_key;
    expect(refreshKey).not.toBe(initialKey);
  });

  it('reuses the adoption idempotency key when a result is uncertain', async () => {
    policyApiMock.adoptGuidelineRevision
      .mockRejectedValueOnce(new Error('temporary adoption failure'))
      .mockResolvedValueOnce(adoption(receipt()));
    const { onAdopted } = renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByTestId('guideline-impact-current-receipt');

    fireEvent.click(screen.getByTestId('guideline-impact-adopt'));
    expect(await screen.findByText(
      'temporary adoption failure',
    )).toBeInTheDocument();
    const firstKey = policyApiMock.adoptGuidelineRevision.mock.calls[0][2]
      .idempotency_key;

    fireEvent.click(screen.getByTestId('guideline-impact-adopt'));

    await waitFor(() =>
      expect(policyApiMock.adoptGuidelineRevision).toHaveBeenCalledTimes(2),
    );
    const retryKey = policyApiMock.adoptGuidelineRevision.mock.calls[1][2]
      .idempotency_key;
    expect(retryKey).toBe(firstKey);
    await waitFor(() => expect(onAdopted).toHaveBeenCalledTimes(1));
  });

  it('invalidates a stale adoption receipt on conflict and requires a fresh preview', async () => {
    policyApiMock.adoptGuidelineRevision.mockRejectedValue(
      new PolicyGovernanceApiError({
        message: 'conflict',
        status: 409,
        kind: 'conflict',
        code: 'conflict',
      }),
    );
    renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByTestId('guideline-impact-current-receipt');

    fireEvent.click(screen.getByTestId('guideline-impact-adopt'));

    expect(
      await screen.findByTestId('guideline-impact-conflict'),
    ).toHaveTextContent('No board change was applied');
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
    expect(
      screen.queryByTestId('guideline-impact-current-receipt'),
    ).not.toBeInTheDocument();
  });

  it('treats a legacy HTTP 400 stale reason as a conflict', async () => {
    policyApiMock.adoptGuidelineRevision.mockRejectedValue(
      new PolicyGovernanceApiError({
        message: 'stale',
        status: 400,
        kind: 'validation_failed',
        code: 'validation_failed',
        details: { reason_code: 'guideline_impact_stale' },
      }),
    );
    renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByTestId('guideline-impact-current-receipt');

    fireEvent.click(screen.getByTestId('guideline-impact-adopt'));

    expect(
      await screen.findByTestId('guideline-impact-conflict'),
    ).toHaveTextContent('No board change was applied');
    expect(screen.getByTestId('guideline-impact-adopt')).toBeDisabled();
  });

  it('rejects a partially mismatched adoption response', async () => {
    const expectedReceipt = receipt();
    policyApiMock.adoptGuidelineRevision.mockResolvedValue({
      ...adoption(expectedReceipt),
      receipt: {
        ...expectedReceipt,
        waiver_snapshot_digest: digest('9'),
      },
    });
    const { onAdopted, onClose } = renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByTestId('guideline-impact-current-receipt');

    fireEvent.click(screen.getByTestId('guideline-impact-adopt'));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'board update response could not be verified',
    );
    expect(onAdopted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('walks the opaque detail cursor without exposing or interpreting it', async () => {
    policyApiMock.listGuidelineImpactItems
      .mockResolvedValueOnce({
        items: [pageItem('first')],
        limit: 50,
        has_more: true,
        next_cursor: 'opaque-server-token',
      })
      .mockResolvedValueOnce({
        items: [pageItem('second', 'waiver')],
        limit: 50,
        has_more: false,
        next_cursor: null,
      });
    renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));

    expect(
      await screen.findByTestId('guideline-impact-item-first'),
    ).toBeInTheDocument();
    expect(screen.queryByText('opaque-server-token')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    expect(
      await screen.findByTestId('guideline-impact-item-second'),
    ).toBeInTheDocument();
    expect(policyApiMock.listGuidelineImpactItems).toHaveBeenLastCalledWith(
      'board-1',
      'guideline-1',
      'impact-1',
      expect.objectContaining({
        projection: 'detail',
        cursor: 'opaque-server-token',
      }),
    );
  });

  it('rejects duplicate item identities across cursor pages and offers a restart', async () => {
    policyApiMock.listGuidelineImpactItems
      .mockResolvedValueOnce({
        items: [pageItem('duplicate')],
        limit: 50,
        has_more: true,
        next_cursor: 'opaque-next',
      })
      .mockResolvedValueOnce({
        items: [pageItem('duplicate')],
        limit: 50,
        has_more: false,
        next_cursor: null,
      });
    renderDialog();
    await screen.findByText('All entities');
    fireEvent.click(screen.getByTestId('guideline-impact-preview'));
    await screen.findByTestId('guideline-impact-item-duplicate');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    expect(
      await screen.findByRole('button', { name: 'Restart from newest' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'duplicate item identity',
    );
  });
});
