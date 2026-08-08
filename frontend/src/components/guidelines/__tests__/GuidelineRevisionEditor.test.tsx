import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CONTEXTUAL_HELP_EVENT } from '@/components/help';
import type {
  GuidelineMetric,
  GuidelineRevisionDetail,
} from '@/types/policy-governance';

const policyApiMock = vi.hoisted(() => ({
  listGuidelineRevisions: vi.fn(),
  getGuidelineRevision: vi.fn(),
  createGuidelineRevision: vi.fn(),
  retireGuideline: vi.fn(),
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

import { GuidelineRevisionEditor } from '../GuidelineRevisionEditor';
import {
  newSemanticMetricDraft,
  validateSemanticMetricDraft,
  validateSemanticMetricDrafts,
} from '../semanticMetricEditorModel';

const guideline = {
  id: 'guideline-1',
  title: 'Delivery quality',
  content: 'Legacy projection',
  tags: ['delivery'],
  scope: 'global' as const,
  board_id: null,
  owner_id: 'owner-1',
  version: 2,
  created_at: '2026-07-29T00:00:00Z',
  updated_at: '2026-07-29T01:00:00Z',
};

const metric: GuidelineMetric = {
  metric_id: 'metric-1',
  code: 'evidence_strength',
  title: 'Evidence strength',
  description: 'How strongly evidence supports the proposal.',
  evaluation_rubric: '0 has no evidence; 100 has independently traceable evidence.',
  target_entity_types: ['spec'],
  direction: 'minimum',
  default_threshold: 70,
};

function revision(
  metrics: GuidelineMetric[] = [],
): GuidelineRevisionDetail {
  return {
    projection: 'detail',
    revision_id: 'revision-2',
    guideline_id: guideline.id,
    revision_number: 2,
    semantic_version: '1.1.0',
    title: guideline.title,
    content: 'Ship only after evidence is attached.',
    revision_digest: 'a'.repeat(64),
    metrics,
    created_by: 'author-1',
    created_at: '2026-07-29T01:00:00Z',
    parent_revision_id: 'revision-1',
    tags: ['delivery'],
  };
}

function authority(latest: GuidelineRevisionDetail) {
  return {
    guideline: {
      guideline_id: guideline.id,
      owner_id: guideline.owner_id,
      scope: 'global',
      created_at: guideline.created_at,
      context_scope: 'all',
    },
    revision: {
      revision_id: latest.revision_id,
      guideline_id: latest.guideline_id,
      revision_number: latest.revision_number,
      semantic_version: latest.semantic_version,
      title: latest.title,
      content: latest.content,
      revision_digest: latest.revision_digest,
      metrics: latest.metrics,
      created_by: latest.created_by,
      created_at: latest.created_at,
      parent_revision_id: latest.parent_revision_id,
      tags: latest.tags,
    },
    head: {
      guideline_id: guideline.id,
      revision_id: latest.revision_id,
      revision_number: latest.revision_number,
      semantic_version: latest.semantic_version,
      head_revision: latest.revision_number,
      updated_at: latest.created_at,
    },
  };
}

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

function renderEditor(
  latest = revision(),
  initialSection?: 'metrics',
) {
  policyApiMock.listGuidelineRevisions.mockResolvedValue({
    items: [latest],
    limit: 10,
    has_more: false,
  });
  policyApiMock.getGuidelineRevision.mockResolvedValue(authority(latest));
  return render(
    <GuidelineRevisionEditor
      boardId="board-1"
      guideline={guideline}
      adoptedRevision={{
        semanticVersion: '1.0.0',
        revisionId: 'revision-1',
        bindingRevision: 4,
      }}
      initialSection={initialSection}
      onClose={vi.fn()}
      onChanged={vi.fn()}
    />,
  );
}

describe('GuidelineRevisionEditor semantic authoring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.isLoading = false;
    permissionState.error = null;
    permissionState.ownerReviewRequired = false;
    grant(
      'guidelines.revisions.read',
      'guidelines.revisions.create',
      'guidelines.revisions.retire',
      'guidelines.metrics.author',
    );
    policyApiMock.createGuidelineRevision.mockResolvedValue({
      revision_id: 'revision-3',
      revision: '1.2.0',
      revision_digest: 'b'.repeat(64),
      metrics: [],
    });
  });

  it('shows fixed Confidence and removes deterministic authoring controls', async () => {
    renderEditor(revision([metric]));

    expect(await screen.findByText('Semantic metrics')).toBeInTheDocument();
    expect(screen.getByTestId('system-confidence-metric')).toHaveTextContent(
      'Confidence',
    );
    expect(screen.getByTestId('system-confidence-metric')).toHaveTextContent(
      'System-owned',
    );
    expect(screen.getByText('metric-1')).toBeInTheDocument();
    expect(screen.queryByText('Policy class')).not.toBeInTheDocument();
    expect(screen.queryByText('Deterministic predicates')).not.toBeInTheDocument();
    expect(screen.queryByText('Operator')).not.toBeInTheDocument();
    expect(screen.queryByText('Code')).not.toBeInTheDocument();
  });

  it('creates an ordered semantic revision with the current head fence', async () => {
    renderEditor();
    await screen.findByText('Context-only guideline');

    fireEvent.click(screen.getByTestId('add-semantic-metric'));
    fireEvent.change(screen.getByLabelText('Metric title'), {
      target: { value: 'User value clarity' },
    });
    fireEvent.change(screen.getByLabelText('Description'), {
      target: { value: 'How clearly the user outcome is defined.' },
    });
    fireEvent.change(screen.getByLabelText('Evaluation rubric'), {
      target: {
        value: '0 has no outcome; 70 has a measurable outcome; 100 has traceable evidence.',
      },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Add Spec metric target' }),
    );
    fireEvent.click(
      screen.getByRole('button', { name: /Lower is better/i }),
    );
    fireEvent.change(
      screen.getByLabelText('Custom metric 1 default threshold value'),
      { target: { value: '30' } },
    );
    fireEvent.change(screen.getByLabelText('Version bump'), {
      target: { value: 'minor' },
    });

    const create = screen.getByTestId('create-guideline-revision');
    expect(create).toBeEnabled();
    fireEvent.click(create);

    await waitFor(() => {
      expect(policyApiMock.createGuidelineRevision).toHaveBeenCalledTimes(1);
    });
    expect(policyApiMock.createGuidelineRevision.mock.calls[0][0])
      .toBe('board-1');
    expect(policyApiMock.createGuidelineRevision.mock.calls[0][1])
      .toBe(guideline.id);
    const request = policyApiMock.createGuidelineRevision.mock.calls[0][2];
    expect(request).toEqual({
      expected_head_revision: 2,
      version_bump: 'minor',
      content: {
        title: 'Delivery quality',
        body: 'Ship only after evidence is attached.',
      },
      metrics: [
        expect.objectContaining({
          code: 'user_value_clarity',
          title: 'User value clarity',
          description: 'How clearly the user outcome is defined.',
          evaluation_rubric:
            '0 has no outcome; 70 has a measurable outcome; 100 has traceable evidence.',
          target_entity_types: ['spec'],
          direction: 'maximum',
          default_threshold: 30,
        }),
      ],
    });
    expect(request.metrics).toEqual([
      expect.objectContaining({
        code: 'user_value_clarity',
        title: 'User value clarity',
        description: 'How clearly the user outcome is defined.',
        evaluation_rubric:
          '0 has no outcome; 70 has a measurable outcome; 100 has traceable evidence.',
        target_entity_types: ['spec'],
        direction: 'maximum',
        default_threshold: 30,
      }),
    ]);
    expect(request.metrics).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: 'confidence' }),
      ]),
    );
    expect(request).not.toHaveProperty('patch');
    expect(request).not.toHaveProperty('tags');
    expect(request).not.toHaveProperty('declared_semantic_version');
    expect(request).not.toHaveProperty('idempotency_key');
  });

  it('publishes an empty metrics array when returning to context-only', async () => {
    renderEditor(revision([metric]));
    await screen.findByTestId('semantic-metric-editor-0');

    fireEvent.click(
      screen.getByRole('button', { name: 'Remove custom metric 1' }),
    );
    fireEvent.click(screen.getByTestId('create-guideline-revision'));

    await waitFor(() => {
      expect(policyApiMock.createGuidelineRevision).toHaveBeenCalledWith(
        'board-1',
        guideline.id,
        {
          expected_head_revision: 2,
          version_bump: 'patch',
          content: {
            title: 'Delivery quality',
            body: 'Ship only after evidence is attached.',
          },
          metrics: [],
        },
      );
    });
  });

  it('matches Core metric-code syntax and case-insensitive reservations', () => {
    const draft = {
      ...newSemanticMetricDraft(),
      metricId: 'metric-1',
      title: 'Traceability',
      code: 'Traceability.v2:API-check',
      description: 'Rates traceability.',
      evaluationRubric: '0 is absent; 100 is complete.',
      targetEntityTypes: ['spec' as const],
    };
    expect(validateSemanticMetricDraft(draft)).toBeNull();

    const reserved = { ...draft, metricId: 'metric-2', code: 'Confidence' };
    expect(validateSemanticMetricDraft(reserved)).toMatch(/system-owned/i);

    const duplicate = {
      ...draft,
      metricId: 'metric-2',
      code: 'traceability.V2:api-CHECK',
    };
    expect(validateSemanticMetricDrafts([draft, duplicate]))
      .toMatch(/keys must be unique/i);
  });

  it('rejects a threshold outside 0..100', async () => {
    renderEditor(revision([metric]));
    await screen.findByTestId('semantic-metric-editor-0');

    fireEvent.change(
      screen.getByLabelText('Custom metric 1 default threshold value'),
      { target: { value: '101' } },
    );

    expect(
      screen.getAllByText(/whole number from 0 to 100/i).length,
    ).toBeGreaterThan(0);
    expect(screen.getByTestId('create-guideline-revision')).toBeDisabled();
  });

  it('opens the dedicated semantic help from the editor', async () => {
    const listener = vi.fn();
    window.addEventListener(CONTEXTUAL_HELP_EVENT, listener);
    renderEditor(revision(), 'metrics');
    await screen.findByText('Semantic metrics');

    fireEvent.click(screen.getByTestId('semantic-metrics-help'));

    expect(listener).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { sectionId: 'semantic-guideline-metrics' },
      }),
    );
    window.removeEventListener(CONTEXTUAL_HELP_EVENT, listener);
  });

  it('fails closed when revision-create authority is absent', async () => {
    grant('guidelines.revisions.read');
    renderEditor();

    expect(await screen.findByText(/Revision history is read-only/i))
      .toBeInTheDocument();
    expect(screen.getByTestId('add-semantic-metric')).toBeDisabled();
    expect(screen.getByTestId('create-guideline-revision')).toBeDisabled();
  });

  it('preserves current metrics in a text-only revision without metric-author authority', async () => {
    grant(
      'guidelines.revisions.read',
      'guidelines.revisions.create',
    );
    renderEditor(revision([metric]));

    expect(
      await screen.findByTestId('semantic-metrics-readonly'),
    ).toHaveTextContent(
      /guidelines\.metrics\.author.*spec\.entity\.edit_fields/s,
    );
    expect(screen.getByTestId('add-semantic-metric')).toBeDisabled();
    expect(screen.getByLabelText('Metric title')).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Remove custom metric 1' }),
    ).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Guideline body'), {
      target: {
        value: 'Ship only after evidence is independently attached.',
      },
    });
    const create = screen.getByTestId('create-guideline-revision');
    expect(create).toBeEnabled();
    fireEvent.click(create);

    await waitFor(() => {
      expect(policyApiMock.createGuidelineRevision).toHaveBeenCalledWith(
        'board-1',
        guideline.id,
        {
          expected_head_revision: 2,
          version_bump: 'patch',
          content: {
            title: 'Delivery quality',
            body: 'Ship only after evidence is independently attached.',
          },
          metrics: [metric],
        },
      );
    });
  });

  it('fails closed when metric-author authority is lost after editing metrics', async () => {
    renderEditor(revision([metric]));
    await screen.findByTestId('semantic-metric-editor-0');

    fireEvent.change(screen.getByLabelText('Metric title'), {
      target: { value: 'Changed evidence strength' },
    });
    permissionState.allowed.delete('guidelines.metrics.author');
    fireEvent.change(screen.getByLabelText('Guideline body'), {
      target: { value: 'Trigger an authority-aware rerender.' },
    });

    expect(
      await screen.findByTestId('semantic-metrics-readonly'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('create-guideline-revision')).toBeDisabled();
    expect(policyApiMock.createGuidelineRevision).not.toHaveBeenCalled();
  });
});
