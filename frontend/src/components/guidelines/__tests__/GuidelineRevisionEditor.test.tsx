import {
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CONTEXTUAL_HELP_EVENT } from '@/components/help';
import type { GuidelineRevisionDetail } from '@/types/policy-governance';

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

import {
  GuidelineRevisionEditor,
  type GuidelineSuccessorOption,
} from '../GuidelineRevisionEditor';
import { PolicyGovernanceApiError } from '@/services/policy-governance-api';

const guideline = {
  id: 'guideline-1',
  title: 'Delivery policy',
  content: 'Legacy projection',
  tags: ['delivery'],
  scope: 'global' as const,
  board_id: null,
  owner_id: 'owner-1',
  version: 2,
  created_at: '2026-07-29T00:00:00Z',
  updated_at: '2026-07-29T01:00:00Z',
};

function revision({
  id = 'revision-2',
  number = 2,
  semanticVersion = '1.1.0',
  title = 'Delivery policy',
  rules = [],
  tags = ['delivery'],
}: Partial<{
  id: string;
  number: number;
  semanticVersion: string;
  title: string;
  rules: GuidelineRevisionDetail['rules'];
  tags: string[];
}> = {}): GuidelineRevisionDetail {
  return {
    projection: 'detail',
    revision_id: id,
    guideline_id: guideline.id,
    revision_number: number,
    semantic_version: semanticVersion,
    title,
    content: 'Ship only after evidence is attached.',
    content_digest: 'a'.repeat(64),
    rules,
    created_by: 'author-1',
    created_at: '2026-07-29T01:00:00Z',
    parent_revision_id: number > 1 ? 'revision-1' : undefined,
    tags,
  };
}

const blockingRule: GuidelineRevisionDetail['rules'][number] = {
  rule_id: 'rule-1',
  code: 'require_evidence',
  title: 'Require evidence',
  description: 'A card needs evidence.',
  target_entity_types: ['card'],
  predicates: [{
    predicate_code: 'count_gte',
    parameters: [
      ['fact', 'labels'],
      ['value', 1],
    ],
  }],
  enforcement: 'blocking',
  operator: 'all',
  waivable: false,
  policy_class: 'standard',
};

function authority(
  latest: GuidelineRevisionDetail,
  retirement?: 'retired' | 'superseded',
) {
  return {
    guideline: {
      guideline_id: guideline.id,
      owner_id: guideline.owner_id,
      scope: 'global',
      created_at: guideline.created_at,
      context_scope: 'all',
    },
    revision: {
      ...latest,
      projection: undefined,
      parent_revision_id: latest.parent_revision_id,
    },
    head: {
      guideline_id: guideline.id,
      revision_id: latest.revision_id,
      revision_number: latest.revision_number,
      semantic_version: latest.semantic_version,
      head_revision: latest.revision_number,
      updated_at: latest.created_at,
    },
    ...(retirement
      ? {
          retirement: {
            retirement_id: 'retirement-1',
            guideline_id: guideline.id,
            status: retirement,
            retired_revision_id: latest.revision_id,
            retired_revision_number: latest.revision_number,
            retired_semantic_version: latest.semantic_version,
            retired_revision_digest: latest.content_digest,
            retired_head_revision: latest.revision_number,
            reason: 'No longer current.',
            retired_by: 'owner-1',
            retired_at: latest.created_at,
          },
        }
      : {}),
  };
}

function page(
  items: GuidelineRevisionDetail[],
  nextCursor?: string,
) {
  return nextCursor
    ? {
        items,
        limit: 10,
        has_more: true as const,
        next_cursor: nextCursor,
      }
    : {
        items,
        limit: 10,
        has_more: false as const,
      };
}

const successors: GuidelineSuccessorOption[] = [{
  guidelineId: 'guideline-2',
  title: 'Replacement policy',
  semanticVersion: '2.0.0',
}];

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

function renderEditor({
  latest = revision(),
  onClose = vi.fn(),
  onChanged = vi.fn(),
  retirement,
}: {
  latest?: GuidelineRevisionDetail;
  onClose?: () => void;
  onChanged?: () => void | Promise<void>;
  retirement?: 'retired' | 'superseded';
} = {}) {
  policyApiMock.listGuidelineRevisions.mockResolvedValue(page([latest]));
  policyApiMock.getGuidelineRevision.mockResolvedValue(
    authority(latest, retirement),
  );
  return {
    ...render(
      <GuidelineRevisionEditor
        boardId="board-1"
        guideline={guideline}
        adoptedRevision={{
          semanticVersion: '1.0.0',
          revisionId: 'revision-1',
          bindingRevision: 4,
        }}
        successorOptions={successors}
        onClose={onClose}
        onChanged={onChanged}
      />,
    ),
    onClose,
    onChanged,
  };
}

describe('GuidelineRevisionEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.isLoading = false;
    permissionState.error = null;
    permissionState.ownerReviewRequired = false;
    grant(
      'guidelines.revisions.read',
      'guidelines.revisions.create',
      'guidelines.rules.author_blocking',
      'guidelines.revisions.retire',
    );
  });

  it('separates all-context from executable targets and shows adopted/latest plus blocking state', async () => {
    const latest = revision({ rules: [blockingRule] });
    renderEditor({ latest });

    expect(await screen.findByText('All entities')).toBeInTheDocument();
    expect(screen.getByText('v1.0.0')).toBeInTheDocument();
    expect(screen.getAllByText('v1.1.0')).not.toHaveLength(0);
    expect(screen.getByText('Update available')).toBeInTheDocument();
    expect(screen.getAllByText('card')).not.toHaveLength(0);
    expect(screen.getByText('Contains blocking rules')).toBeInTheDocument();
    expect(screen.getByText(/policy\/v1 operators/)).toBeInTheDocument();
    expect(screen.getByTestId('guideline-revision-help'))
      .toHaveTextContent('Revision guide');
  });

  it('opens canonical policy Help from the revision editor', () => {
    const helpListener = vi.fn();
    window.addEventListener(CONTEXTUAL_HELP_EVENT, helpListener, {
      once: true,
    });
    renderEditor();

    fireEvent.click(screen.getByTestId('guideline-revision-help'));

    expect(helpListener).toHaveBeenCalledWith(
      expect.objectContaining({
        detail: { sectionId: 'policy-governance' },
      }),
    );
  });

  it('uses exact revision identity for update availability even when SemVer is unchanged', async () => {
    renderEditor({
      latest: revision({
        id: 'revision-2',
        semanticVersion: '1.0.0',
      }),
    });

    expect(await screen.findByText('All entities')).toBeInTheDocument();
    expect(screen.getByText('Update available')).toBeInTheDocument();
  });

  it('fails closed while permission authority is unavailable and never reads history', async () => {
    permissionState.error = new Error('permission service down');
    grant();
    renderEditor();

    expect(
      await screen.findByText(/Permission status is unavailable/),
    ).toBeInTheDocument();
    expect(policyApiMock.listGuidelineRevisions).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', {
      name: 'Create immutable revision',
    })).not.toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: 'Retire guideline',
    })).toBeDisabled();
    expect(screen.getByTestId('guideline-revision-help'))
      .toHaveTextContent('Revision guide');
  });

  it('allows a text-only revision without rule-authoring authority and omits rules from the patch', async () => {
    grant('guidelines.revisions.read', 'guidelines.revisions.create');
    policyApiMock.createGuidelineRevision.mockResolvedValue({
      status: 'noop',
    });
    renderEditor();

    const title = await screen.findByLabelText('Title');
    fireEvent.change(title, { target: { value: 'Updated delivery policy' } });
    fireEvent.click(screen.getByTestId('create-guideline-revision'));

    await waitFor(() => {
      expect(policyApiMock.createGuidelineRevision).toHaveBeenCalledTimes(1);
    });
    const request = policyApiMock.createGuidelineRevision.mock.calls[0][2];
    expect(request.patch).toEqual({ title: 'Updated delivery policy' });
    expect(request.patch).not.toHaveProperty('rules');
    expect(screen.getByTestId('add-policy-rule')).toBeDisabled();
  });

  it('keeps boolean and enum predicate values explicit instead of displaying phantom defaults', async () => {
    renderEditor();
    fireEvent.click(await screen.findByTestId('add-policy-rule'));

    const operator = screen.getByLabelText(
      'Rule 1 predicate 1 operator',
    );
    fireEvent.change(operator, { target: { value: 'eq' } });
    expect(
      screen.getByLabelText('Rule 1 predicate 1 value'),
    ).toHaveValue('');

    fireEvent.change(
      screen.getByLabelText('Rule 1 predicate 1 fact'),
      { target: { value: 'resource_gate_ready' } },
    );
    fireEvent.change(operator, { target: { value: 'eq' } });
    const booleanValue = screen.getByLabelText(
      'Rule 1 predicate 1 value',
    );
    expect(booleanValue).toHaveValue('');
    fireEvent.change(booleanValue, { target: { value: 'true' } });
    expect(booleanValue).toHaveValue('true');
  });

  it('reuses the idempotency key for an unchanged under-bump retry and keeps the draft editable', async () => {
    const underBump = new PolicyGovernanceApiError({
      status: 400,
      kind: 'under_bump',
      code: 'under_bump',
      message: 'Declared version is too low.',
      details: { minimum_bump: 'major' },
    });
    policyApiMock.createGuidelineRevision.mockRejectedValue(underBump);
    renderEditor();

    fireEvent.change(await screen.findByLabelText('Title'), {
      target: { value: 'Breaking delivery policy' },
    });
    fireEvent.change(screen.getByLabelText('Declared semantic version'), {
      target: { value: '1.1.1' },
    });
    const save = screen.getByTestId('create-guideline-revision');
    fireEvent.click(save);
    await screen.findByText(/Minimum required: major/);
    fireEvent.click(save);

    await waitFor(() => {
      expect(policyApiMock.createGuidelineRevision).toHaveBeenCalledTimes(2);
    });
    const first = policyApiMock.createGuidelineRevision.mock.calls[0][2];
    const second = policyApiMock.createGuidelineRevision.mock.calls[1][2];
    expect(second.idempotency_key).toBe(first.idempotency_key);
    expect(screen.getByLabelText('Title')).toHaveValue(
      'Breaking delivery policy',
    );
  });

  it('deduplicates paged history and keeps the editor usable when a cursor repeats', async () => {
    const latest = revision();
    const older = revision({
      id: 'revision-1',
      number: 1,
      semanticVersion: '1.0.0',
      title: 'Original policy',
    });
    policyApiMock.listGuidelineRevisions
      .mockResolvedValueOnce(page([latest], 'opaque-cursor'))
      .mockResolvedValueOnce(page([latest, older], 'opaque-cursor'));
    policyApiMock.getGuidelineRevision.mockResolvedValue(authority(latest));

    renderEditor({ latest });
    fireEvent.click(await screen.findByText('Load older revisions'));

    expect(
      await screen.findByText(/server returned a repeated cursor/i),
    ).toBeInTheDocument();
    expect(screen.getAllByText('Delivery policy')).toHaveLength(2);
    expect(screen.getByText('Original policy')).toBeInTheDocument();
    expect(screen.getByText('New revision')).toBeInTheDocument();
  });

  it('restarts history after an invalid cursor without hiding the loaded draft', async () => {
    const latest = revision();
    const invalidCursor = new PolicyGovernanceApiError({
      status: 400,
      kind: 'invalid_cursor',
      code: 'invalid_cursor',
      message: 'Cursor invalid.',
    });
    policyApiMock.listGuidelineRevisions
      .mockResolvedValueOnce(page([latest], 'opaque-cursor'))
      .mockRejectedValueOnce(invalidCursor)
      .mockResolvedValueOnce(page([latest]));
    policyApiMock.getGuidelineRevision.mockResolvedValue(authority(latest));

    renderEditor({ latest });
    fireEvent.click(await screen.findByText('Load older revisions'));
    expect(await screen.findByText(/cursor expired/i)).toBeInTheDocument();
    expect(screen.getByText('New revision')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Restart history'));

    await waitFor(() => {
      expect(policyApiMock.listGuidelineRevisions).toHaveBeenCalledTimes(3);
    });
  });

  it('uses a catalog successor and stable retirement identifiers across retries', async () => {
    policyApiMock.retireGuideline
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({});
    const { onClose } = renderEditor();

    fireEvent.click(await screen.findByRole('button', {
      name: 'Retire guideline',
    }));
    fireEvent.click(screen.getByLabelText('Superseded'));
    fireEvent.change(screen.getByLabelText('Successor guideline'), {
      target: { value: 'guideline-2' },
    });
    fireEvent.change(screen.getByLabelText('Reason'), {
      target: { value: 'Replacement is authoritative.' },
    });
    const confirm = screen.getByRole('button', {
      name: 'Confirm retirement',
    });
    fireEvent.click(confirm);
    expect(await screen.findAllByText('temporary failure')).not.toHaveLength(0);
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(policyApiMock.retireGuideline).toHaveBeenCalledTimes(2);
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    const first = policyApiMock.retireGuideline.mock.calls[0][2];
    const second = policyApiMock.retireGuideline.mock.calls[1][2];
    expect(second).toEqual(first);
    expect(first).toMatchObject({
      status: 'superseded',
      superseded_by_guideline_id: 'guideline-2',
      reason: 'Replacement is authoritative.',
    });
  });

  it('does not offer a second retirement for an already retired guideline', async () => {
    const latest = revision();
    renderEditor({ latest, retirement: 'retired' });

    const retired = await screen.findByRole('button', {
      name: 'Guideline retired',
    });
    expect(retired).toBeDisabled();
  });
});
