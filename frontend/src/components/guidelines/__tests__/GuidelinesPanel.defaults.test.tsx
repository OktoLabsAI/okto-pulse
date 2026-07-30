import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  DefaultGuidelineCandidate,
  DefaultGuidelineRevisionPin,
} from '@/types';

const apiMock = vi.hoisted(() => ({
  getBoardGuidelines: vi.fn(),
  listDefaultGuidelineCandidates: vi.fn(),
  updateDefaultGuidelineRefs: vi.fn(),
  createDefaultBoardConfigVersion: vi.fn(),
  createGuideline: vi.fn(),
  unlinkGuidelineFromBoard: vi.fn(),
  listGuidelines: vi.fn(),
}));
const policyApiMock = vi.hoisted(() => ({
  getGuidelineRevision: vi.fn(),
  exportGuidelinePolicy: vi.fn(),
  importGuidelinePolicy: vi.fn(),
}));
const permissionState = vi.hoisted(() => ({
  allowed: new Set<string>(),
}));
const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));
vi.mock('@/services/policy-governance-api', () => ({
  PolicyGovernanceApiError: class extends Error {},
  usePolicyGovernanceApi: () => policyApiMock,
}));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Full Control',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (flag: string) => permissionState.allowed.has(flag),
  }),
}));
vi.mock('@/components/policy-compliance/PolicyWaiverPanel', () => ({
  PolicyWaiverPanel: ({ boardId }: { boardId: string }) => (
    <div data-testid="mock-policy-waiver-panel">
      Waiver management for {boardId}
    </div>
  ),
}));
vi.mock('react-hot-toast', () => ({ default: toastMock }));
vi.mock('../GuidelineImpactDialog', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  return {
    GuidelineImpactDialog: (props: {
      targetRevisionId: string;
      targetSemanticVersion: string;
      onClose: () => void;
    }) => {
      const closeRef = React.useRef<HTMLButtonElement>(null);
      React.useEffect(() => {
        const opener = document.activeElement as HTMLElement | null;
        closeRef.current?.focus();
        return () => opener?.focus();
      }, []);
      return (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Guideline impact"
          data-testid="mock-guideline-impact"
          data-target-revision={props.targetRevisionId}
          data-target-version={props.targetSemanticVersion}
        >
          <button ref={closeRef} type="button" onClick={props.onClose}>
            Close impact
          </button>
        </div>
      );
    },
  };
});

import { GuidelinesPanel } from '../GuidelinesPanel';

const digest = (character: string) => character.repeat(64);

function pin(
  revisionId: string,
  revisionNumber: number,
  semanticVersion: string,
  digestCharacter: string,
): DefaultGuidelineRevisionPin {
  return {
    revision_id: revisionId,
    revision_number: revisionNumber,
    semantic_version: semanticVersion,
    revision_digest: digest(digestCharacter),
  };
}

function candidate({
  id,
  head = pin(`${id}-r2`, 2, '2.0.0', 'b'),
  defaultRevision = null,
  retired = false,
  priority = null,
}: {
  id: string;
  head?: DefaultGuidelineRevisionPin;
  defaultRevision?: DefaultGuidelineRevisionPin | null;
  retired?: boolean;
  priority?: number | null;
}): DefaultGuidelineCandidate {
  return {
    guideline_id: id,
    title: `Guideline ${id}`,
    scope: 'global',
    guideline_version: head.revision_number,
    revision_id: head.revision_id,
    revision_number: head.revision_number,
    semantic_version: head.semantic_version,
    revision_digest: head.revision_digest,
    head_revision: head,
    default_revision: defaultRevision,
    retired,
    eligible: !retired,
    eligibility_reason: retired ? 'guideline_retired' : null,
    is_default: defaultRevision !== null,
    priority,
  };
}

const guideline = (
  id: string,
  scope: 'global' | 'inline' = 'global',
) => ({
  id,
  title: `Guideline ${id}`,
  content: `${id} content`,
  tags: null,
  scope,
  board_id: scope === 'inline' ? 'b1' : null,
  owner_id: 'owner',
  version: 1,
  created_at: '2026-07-29T00:00:00Z',
  updated_at: '2026-07-29T00:00:00Z',
});

function revisionAuthority({
  revisionId,
  revisionNumber,
  semanticVersion,
  headRevisionId,
  headRevisionNumber,
  headSemanticVersion,
}: {
  revisionId: string;
  revisionNumber: number;
  semanticVersion: string;
  headRevisionId: string;
  headRevisionNumber: number;
  headSemanticVersion: string;
}) {
  return {
    guideline: {
      guideline_id: 'g-inline',
      owner_id: 'owner',
      scope: 'inline',
      board_id: 'b1',
      created_at: '2026-07-29T00:00:00Z',
      context_scope: 'all',
    },
    revision: {
      revision_id: revisionId,
      guideline_id: 'g-inline',
      revision_number: revisionNumber,
      semantic_version: semanticVersion,
      title: 'Inline guideline',
      content: 'Inline context',
      content_digest: digest(revisionId === headRevisionId ? 'c' : 'a'),
      rules: [],
      created_by: 'owner',
      created_at: '2026-07-29T00:00:00Z',
      ...(revisionNumber > 1
        ? { parent_revision_id: 'g-inline-r1' }
        : {}),
      tags: [],
    },
    head: {
      guideline_id: 'g-inline',
      revision_id: headRevisionId,
      revision_number: headRevisionNumber,
      semantic_version: headSemanticVersion,
      head_revision: headRevisionNumber,
      updated_at: '2026-07-29T00:00:01Z',
    },
  };
}

describe('GuidelinesPanel immutable catalog and defaults', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.allowed = new Set([
      'guidelines.revisions.read',
      'guidelines.impact.preview',
      'guidelines.adoption.manage',
    ]);
    apiMock.getBoardGuidelines.mockResolvedValue([
      {
        id: 'e1',
        binding_id: 'binding-g1',
        binding_revision: 3,
        binding_state: 'active',
        default_enforcement: 'advisory',
        source_kind: 'native',
        priority: 0,
        scope: 'global',
        guideline: {
          ...guideline('g1'),
          semantic_version: '1.0.0',
          revision_id: 'g1-r1',
          revision_digest: digest('a'),
        },
      },
      {
        id: 'e2',
        binding_id: 'binding-inline',
        binding_revision: 1,
        binding_state: 'active',
        default_enforcement: 'blocking',
        source_kind: 'native',
        priority: 1,
        scope: 'inline',
        guideline: {
          ...guideline('g-inline', 'inline'),
          semantic_version: '1.0.0',
          revision_id: 'g-inline-r1',
          revision_digest: digest('a'),
        },
      },
    ]);
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 'template-1',
      template_version: 1,
      candidates: [candidate({ id: 'g1' })],
    });
    apiMock.listGuidelines.mockResolvedValue([]);
    apiMock.updateDefaultGuidelineRefs.mockResolvedValue({});
    apiMock.createDefaultBoardConfigVersion.mockResolvedValue({});
    apiMock.createGuideline.mockResolvedValue(guideline('g-created'));
    apiMock.unlinkGuidelineFromBoard.mockResolvedValue({});
    policyApiMock.getGuidelineRevision
      .mockResolvedValueOnce(revisionAuthority({
        revisionId: 'g-inline-r1',
        revisionNumber: 1,
        semanticVersion: '1.0.0',
        headRevisionId: 'g-inline-r2',
        headRevisionNumber: 2,
        headSemanticVersion: '2.0.0',
      }))
      .mockResolvedValueOnce(revisionAuthority({
        revisionId: 'g-inline-r2',
        revisionNumber: 2,
        semanticVersion: '2.0.0',
        headRevisionId: 'g-inline-r2',
        headRevisionNumber: 2,
        headSemanticVersion: '2.0.0',
      }));
  });

  it('exposes central waiver management only with its exact read capability', async () => {
    const { unmount } = render(
      <GuidelinesPanel boardId="b1" onClose={vi.fn()} />,
    );
    await waitFor(() =>
      expect(apiMock.getBoardGuidelines).toHaveBeenCalledWith('b1'),
    );
    expect(
      screen.queryByRole('button', { name: 'Waivers' }),
    ).not.toBeInTheDocument();
    unmount();

    permissionState.allowed.add('guidelines.waiver.read');
    render(<GuidelinesPanel boardId="b1" onClose={vi.fn()} />);
    await waitFor(() =>
      expect(apiMock.getBoardGuidelines).toHaveBeenCalledWith('b1'),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Waivers' }));
    expect(screen.getByTestId('mock-policy-waiver-panel')).toHaveTextContent(
      'Waiver management for b1',
    );
  });

  it('blocks defaults for inline guidelines and sends the exact head pin for a new global default', async () => {
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));

    const globalButton = await screen.findByTestId('guideline-set-default-g1');
    expect(screen.getByTestId('guideline-set-default-g-inline')).toBeDisabled();
    expect(globalButton).not.toBeDisabled();
    fireEvent.click(globalButton);
    expect(apiMock.updateDefaultGuidelineRefs).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('guideline-default-save'));

    await waitFor(() => {
      expect(apiMock.updateDefaultGuidelineRefs).toHaveBeenCalledWith(
        'template-1',
        [{
          guideline_id: 'g1',
          priority: 1,
          ...pin('g1-r2', 2, '2.0.0', 'b'),
        }],
      );
    });
  });

  it('preserves a historical default pin when another guideline is added', async () => {
    const historical = pin('g1-r1', 1, '1.0.0', 'a');
    const g1 = candidate({
      id: 'g1',
      defaultRevision: historical,
      priority: 4,
    });
    const g2 = candidate({
      id: 'g2',
      head: pin('g2-r3', 3, '3.0.0', 'c'),
    });
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 'template-1',
      template_version: 7,
      candidates: [g1, g2],
    });
    apiMock.listGuidelines.mockResolvedValue([
      guideline('g1'),
      guideline('g2'),
    ]);

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('guideline-set-default-g2'));
    expect(apiMock.updateDefaultGuidelineRefs).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('guideline-default-save'));

    await waitFor(() => {
      expect(apiMock.updateDefaultGuidelineRefs).toHaveBeenCalledWith(
        'template-1',
        [
          { guideline_id: 'g1', priority: 4, ...historical },
          {
            guideline_id: 'g2',
            priority: 5,
            ...pin('g2-r3', 3, '3.0.0', 'c'),
          },
        ],
      );
    });
  });

  it('creates the first active template atomically with its exact default pin', async () => {
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: null,
      template_version: null,
      candidates: [candidate({ id: 'g1' })],
    });
    apiMock.listGuidelines.mockResolvedValue([guideline('g1')]);

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('guideline-set-default-g1'));
    expect(apiMock.createDefaultBoardConfigVersion).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('guideline-default-save'));

    await waitFor(() => {
      expect(apiMock.createDefaultBoardConfigVersion).toHaveBeenCalledWith({
        guideline_default_refs: [{
          guideline_id: 'g1',
          priority: 1,
          ...pin('g1-r2', 2, '2.0.0', 'b'),
        }],
        activate: true,
      });
    });
    expect(apiMock.updateDefaultGuidelineRefs).not.toHaveBeenCalled();
  });

  it('refuses a retired candidate as a new default but allows an existing retired default to be removed', async () => {
    const retired = candidate({ id: 'g-retired', retired: true });
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 'template-1',
      template_version: 1,
      candidates: [retired],
    });
    apiMock.listGuidelines.mockResolvedValue([guideline('g-retired')]);

    const { unmount } = render(
      <GuidelinesPanel boardId="b1" onClose={() => {}} />,
    );
    expect(
      await screen.findByTestId('guideline-set-default-g-retired'),
    ).toBeDisabled();
    unmount();

    const historical = pin('g-retired-r1', 1, '1.0.0', 'd');
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 'template-1',
      template_version: 2,
      candidates: [candidate({
        id: 'g-retired',
        retired: true,
        defaultRevision: historical,
        priority: 2,
      })],
    });
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    const removeButton = await screen.findByTestId(
      'guideline-set-default-g-retired',
    );
    expect(removeButton).not.toBeDisabled();
    fireEvent.click(removeButton);
    expect(apiMock.updateDefaultGuidelineRefs).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('guideline-default-save'));
    await waitFor(() => {
      expect(apiMock.updateDefaultGuidelineRefs).toHaveBeenCalledWith(
        'template-1',
        [],
      );
    });
  });

  it('pages the global catalog in bounded chunks without duplicate rows', async () => {
    const firstPage = Array.from(
      { length: 50 },
      (_, index) => guideline(`g-${index}`),
    );
    apiMock.listGuidelines
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValueOnce([
        guideline('g-49'),
        guideline('g-50'),
      ]);

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    expect(await screen.findByText('Guideline g-0')).toBeInTheDocument();
    expect(apiMock.listGuidelines).toHaveBeenCalledWith(0, 50);

    fireEvent.click(screen.getByTestId('guidelines-load-more'));
    await waitFor(() => {
      expect(apiMock.listGuidelines).toHaveBeenCalledWith(50, 50);
      expect(screen.getByText('Guideline g-50')).toBeInTheDocument();
    });
    expect(screen.getAllByText('Guideline g-49')).toHaveLength(1);
  });

  it('offers immutable revision management and never renders hard-delete actions', async () => {
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));
    fireEvent.click(await screen.findByText('Guideline g-inline'));

    expect(screen.getByText('Edit guideline')).toBeInTheDocument();
    expect(screen.queryByText(/^Delete$/)).not.toBeInTheDocument();
    expect(screen.queryByTitle('Delete')).not.toBeInTheDocument();
  });

  it('expands board rows from the keyboard and reviews inline bindings at the validated latest head', async () => {
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));

    const expand = await screen.findByTestId('guideline-expand-g-inline');
    expect(expand.tagName).toBe('BUTTON');
    expand.focus();
    fireEvent.click(expand);
    expect(expand).toHaveAttribute('aria-expanded', 'true');

    const reviewButton = screen.getByTestId(
      'guideline-review-adoption-g-inline',
    );
    reviewButton.focus();
    fireEvent.click(reviewButton);

    await waitFor(() =>
      expect(policyApiMock.getGuidelineRevision).toHaveBeenCalledTimes(2),
    );
    expect(policyApiMock.getGuidelineRevision.mock.calls[0].slice(0, 3))
      .toEqual(['b1', 'g-inline', 'g-inline-r1']);
    expect(policyApiMock.getGuidelineRevision.mock.calls[1].slice(0, 3))
      .toEqual(['b1', 'g-inline', 'g-inline-r2']);
    const child = await screen.findByTestId('mock-guideline-impact');
    expect(child).toHaveAttribute('data-target-revision', 'g-inline-r2');
    expect(child).toHaveAttribute('data-target-version', '2.0.0');

    const parent = document.querySelector(
      '[aria-labelledby="guidelines-panel-title"]',
    );
    expect(parent).toHaveAttribute('aria-hidden', 'true');
    expect(parent).not.toHaveAttribute('aria-modal');
    expect(screen.getByText('Close impact')).toHaveFocus();

    fireEvent.click(screen.getByText('Close impact'));
    await waitFor(() => {
      expect(parent).not.toHaveAttribute('aria-hidden');
      expect(reviewButton).toHaveFocus();
    });
  });

  it('fails closed when exact binding authority is incomplete', async () => {
    const rows = await apiMock.getBoardGuidelines();
    apiMock.getBoardGuidelines.mockResolvedValue(rows.map(
      (entry: Record<string, unknown>) => {
        if (entry.id !== 'e2') return entry;
        const current = entry.guideline as Record<string, unknown>;
        return {
          ...entry,
          guideline: { ...current, revision_digest: undefined },
        };
      },
    ));
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));
    fireEvent.click(await screen.findByTestId('guideline-expand-g-inline'));

    expect(screen.getByTestId(
      'guideline-review-adoption-g-inline',
    )).toBeDisabled();
  });

  it('gates every remove action on adoption management authority', async () => {
    permissionState.allowed.delete('guidelines.adoption.manage');
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));
    fireEvent.click(await screen.findByTestId('guideline-expand-g1'));

    expect(screen.getByText('Remove from board')).toBeDisabled();
    fireEvent.click(screen.getByText('Remove from board'));
    expect(apiMock.unlinkGuidelineFromBoard).not.toHaveBeenCalled();
  });

  it('reloads default candidates after creating a global guideline', async () => {
    permissionState.allowed.add('guidelines.revisions.create');
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('New Global Guideline'));
    fireEvent.change(screen.getByPlaceholderText('Guideline title'), {
      target: { value: 'Created global' },
    });
    fireEvent.change(screen.getByPlaceholderText(
      'Content (Markdown supported)',
    ), {
      target: { value: 'Created context' },
    });
    fireEvent.click(screen.getByText('Create Global'));

    await waitFor(() => {
      expect(apiMock.createGuideline).toHaveBeenCalledTimes(1);
      expect(apiMock.listDefaultGuidelineCandidates).toHaveBeenCalledTimes(2);
    });
  });

  it('presents Add to board while preserving the governed impact flow', async () => {
    apiMock.listGuidelines.mockResolvedValue([guideline('g-available')]);
    apiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: 'template-1',
      template_version: 1,
      candidates: [candidate({ id: 'g-available' })],
    });
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);

    const adoption = await screen.findByTestId(
      'guideline-adopt-board-g-available',
    );
    expect(adoption).toBeEnabled();
    expect(adoption).toHaveTextContent('Add to board');
    expect(screen.getByText('Not on board')).toBeInTheDocument();
    expect(screen.queryByText(/^Link$/)).not.toBeInTheDocument();
  });

  it('uses prominent board actions and confirms removal before unlinking', async () => {
    const confirm = vi.spyOn(window, 'confirm')
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByText('Board Guidelines'));
    fireEvent.click(await screen.findByTestId('guideline-expand-g1'));

    expect(screen.getByText('Edit guideline')).toBeInTheDocument();
    expect(screen.getByText('Review update')).toBeInTheDocument();
    const remove = screen.getByText('Remove from board');

    fireEvent.click(remove);
    expect(apiMock.unlinkGuidelineFromBoard).not.toHaveBeenCalled();
    fireEvent.click(remove);

    await waitFor(() => {
      expect(apiMock.unlinkGuidelineFromBoard).toHaveBeenCalledWith(
        'b1',
        'g1',
      );
    });
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining(
      'The global guideline and its revision history will be preserved.',
    ));
    confirm.mockRestore();
  });

  it('shows explicit edit and manage actions for a global guideline already on the board', async () => {
    apiMock.listGuidelines.mockResolvedValue([guideline('g1')]);
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);

    expect(
      await screen.findByTestId('guideline-adopt-board-g1'),
    ).toHaveTextContent('Review update');
    expect(screen.getByText(/On board v/)).toBeInTheDocument();
    expect(screen.getByText('Edit guideline')).toBeInTheDocument();
  });
});
