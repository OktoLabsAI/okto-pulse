import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';
import { RefinementModal } from '../RefinementModal';
import type { Refinement } from '@/types';

const apiMock = vi.hoisted(() => ({
  getRefinement: vi.fn(),
  getRefinementKnowledge: vi.fn(),
  listRefinementKnowledge: vi.fn(),
  getEffectiveResources: vi.fn(),
  getArchitectureDesign: vi.fn(),
  listRefinementSnapshots: vi.fn(),
  listRefinementHistory: vi.fn(),
  listRefinementQA: vi.fn(),
  getAllowedTransitions: vi.fn(),
  moveRefinement: vi.fn(),
  deleteRefinement: vi.fn(),
  updateRefinement: vi.fn(),
  deriveSpecFromRefinement: vi.fn(),
}));

const contextSelectorMock = vi.hoisted(() => vi.fn());

const markdownMock = vi.hoisted(() => ({
  exportRefinement: vi.fn(() => '# refinement export'),
  downloadMarkdown: vi.fn(),
  slugify: vi.fn((s: string) => s.toLowerCase().replace(/\s+/g, '-')),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useCurrentBoard: () => ({ id: 'board-1', owner_id: 'owner-1', agents: [] }),
}));

vi.mock('@/lib/exportMarkdown', () => ({
  exportRefinement: markdownMock.exportRefinement,
  downloadMarkdown: markdownMock.downloadMarkdown,
  slugify: markdownMock.slugify,
}));

vi.mock('@/components/traceability', () => ({
  openLineageGraph: vi.fn(),
}));

vi.mock('@/components/architecture', () => ({
  ArchitectureTab: () => <div />,
}));

vi.mock('@/components/resources/ResourceGateSummary', () => ({
  ResourceGateSummary: () => <div />,
}));

vi.mock('@/components/specs/MockupsTab', () => ({
  MockupsTab: () => <div />,
}));

vi.mock('@/components/ideations/IdeationModal', () => ({
  IdeationModal: () => <div />,
}));

vi.mock('@/components/shared/MentionInput', () => ({
  MentionInput: () => <div />,
}));

vi.mock('@/components/shared/ContextSelector', () => ({
  ContextSelector: (props: {
    busy?: boolean;
    knowledgeOnly?: boolean;
    title?: string;
    description?: string;
    items: Array<{ id: string }>;
    knowledgeItems: Array<{
      id: string;
      title: string;
      stale?: boolean;
      origin_class?: string | null;
    }>;
    onConfirm: (
      selectedItems: Array<{ id: string }>,
      title: string,
      choice: {
        action: 'reference' | 'drop';
        knowledgeIds: string[];
        justification: string;
      },
    ) => void | Promise<void>;
  }) => {
    contextSelectorMock(props);
    return (
      <div
        data-testid="context-selector"
        data-busy={props.busy ? 'true' : 'false'}
        data-knowledge-only={props.knowledgeOnly ? 'true' : 'false'}
        data-context-count={String(props.items.length)}
      >
        <span data-testid="selector-description">{props.description}</span>
        <span data-testid="selector-knowledge">
          {props.knowledgeItems
            .map((item) =>
              [
                item.id,
                item.title,
                item.stale ? 'stale' : 'current',
                item.origin_class || 'no-origin',
              ].join('|'),
            )
            .join(',')}
        </span>
        <button
          type="button"
          disabled={props.busy}
          onClick={() => {
            void props.onConfirm(
              [],
              '',
              {
                action: 'reference',
                knowledgeIds: ['root-refinement-kb'],
                justification: 'Required by the derived functional scope',
              },
            );
          }}
        >
          Confirm selector
        </button>
        <button
          type="button"
          disabled={props.busy}
          onClick={() => {
            void props.onConfirm(
              [],
              '',
              {
                action: 'drop',
                knowledgeIds: [],
                justification: 'No Knowledge is relevant to the derived spec',
              },
            );
          }}
        >
          Confirm explicit empty
        </button>
      </div>
    );
  },
  buildRefinementItems: vi.fn(() => []),
}));

vi.mock('@/components/shared/EditableField', () => ({
  EditableField: ({ value, renderView, placeholder }: any) => (
    <div>{value ? renderView(value) : placeholder}</div>
  ),
}));

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

const baseRefinement: Refinement = {
  id: 'refinement-1',
  ideation_id: 'ideation-1',
  board_id: 'board-1',
  title: 'My Refinement',
  description: 'A refinement',
  in_scope: ['in'],
  out_of_scope: ['out'],
  analysis: 'analysis',
  decisions: ['decision'],
  screen_mockups: [],
  architecture_designs: [],
  status: 'review',
  version: 3,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-05-06T10:00:00Z',
  updated_at: '2026-05-06T10:00:00Z',
  labels: [],
  specs: [],
  qa_items: [],
  knowledge_bases: [],
};

// ---------------------------------------------------------------------------
// AC1 — handleMove surfaces backend detail via getErrorMessage
// ---------------------------------------------------------------------------
describe('RefinementModal handleMove error surfacing (AC1)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getRefinement.mockResolvedValue(baseRefinement);
    apiMock.listRefinementSnapshots.mockResolvedValue([]);
    apiMock.listRefinementHistory.mockResolvedValue([]);
    apiMock.listRefinementQA.mockResolvedValue([]);
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'refinement',
      entity_id: 'refinement-1',
      current_status: 'review',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [
        { to_status: 'approved', label: 'Approved', gate: 'none', blocked_reason: null, blocked_facts: null, preconditions: [], capabilities: [], effects: [], reason_codes: [], policy_compliance: false, policy_compliance_decision: null },
        { to_status: 'draft', label: 'Draft', gate: 'none', blocked_reason: null, blocked_facts: null, preconditions: [], capabilities: [], effects: [], reason_codes: [], policy_compliance: false, policy_compliance_decision: null },
        { to_status: 'cancelled', label: 'Cancelled', gate: 'none', blocked_reason: null, blocked_facts: null, preconditions: [], capabilities: [], effects: [], reason_codes: [], policy_compliance: false, policy_compliance_decision: null },
      ],
    });
    apiMock.getArchitectureDesign.mockResolvedValue(null);
  });

  it('shows backend detail string (not fallback) when moveRefinement rejects', async () => {
    const backendDetail = 'Refinement must have at least one in_scope item before moving to review.';
    apiMock.moveRefinement.mockRejectedValue(new Error(backendDetail));

    render(
      <RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />,
    );

    // Wait for modal to load
    await screen.findByText('My Refinement');

    // Click one of the "Move to:" buttons (baseRefinement is in 'review',
    // so next statuses are Approved / Draft / Cancelled)
    fireEvent.click(screen.getByText('Approved'));

    await waitFor(() => {
      expect((toast as any).error).toHaveBeenCalledWith(backendDetail);
    });
    // Must NOT have been called with the old hardcoded fallback
    expect((toast as any).error).not.toHaveBeenCalledWith('Failed to move refinement');
  });

  it.each([
    {
      caseName: 'does not highlight a receipt-backed choice-only answer',
      qa: {
        answer: null,
        selected: ['safe'],
        answered_at: '2026-07-27T12:00:00Z',
      },
      expectedClass: 'bg-gray-200',
    },
    {
      caseName: 'highlights a payload that has no answer receipt',
      qa: {
        answer: 'A non-authoritative payload',
        selected: null,
        answered_at: null,
      },
      expectedClass: 'bg-amber-200',
    },
  ])('$caseName', async ({ qa, expectedClass }) => {
    apiMock.getRefinement.mockResolvedValue({
      ...baseRefinement,
      qa_items: [
        {
          id: 'qa-1',
          refinement_id: 'refinement-1',
          question: 'Which rollout?',
          question_type: 'single_choice',
          choices: [{ id: 'safe', label: 'Safe rollout' }],
          allow_free_text: false,
          asked_by: 'agent-1',
          answered_by: qa.answered_at ? 'user-1' : null,
          created_at: '2026-07-27T11:00:00Z',
          ...qa,
        },
      ],
    });

    render(
      <RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />,
    );

    await screen.findByText('My Refinement');
    const qaTab = screen.getByRole('tab', { name: /Q&A/ });
    expect(qaTab.querySelector('.rounded-full')).toHaveClass(expectedClass);
  });
});

describe('RefinementModal Markdown export', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getRefinement.mockResolvedValue(baseRefinement);
    apiMock.getRefinementKnowledge.mockImplementation((_rid: string, kbId: string) =>
      Promise.resolve({ id: kbId, content: 'kb content' }),
    );
    apiMock.listRefinementSnapshots.mockResolvedValue([]);
    apiMock.listRefinementHistory.mockResolvedValue([]);
    apiMock.listRefinementQA.mockResolvedValue([]);
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'refinement',
      entity_id: 'refinement-1',
      current_status: 'review',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [
        { to_status: 'approved', label: 'Approved', gate: 'none', blocked_reason: null, blocked_facts: null, preconditions: [], capabilities: [], effects: [], reason_codes: [], policy_compliance: false, policy_compliance_decision: null },
        { to_status: 'draft', label: 'Draft', gate: 'none', blocked_reason: null, blocked_facts: null, preconditions: [], capabilities: [], effects: [], reason_codes: [], policy_compliance: false, policy_compliance_decision: null },
        { to_status: 'cancelled', label: 'Cancelled', gate: 'none', blocked_reason: null, blocked_facts: null, preconditions: [], capabilities: [], effects: [], reason_codes: [], policy_compliance: false, policy_compliance_decision: null },
      ],
    });
    apiMock.getArchitectureDesign.mockImplementation((id: string) =>
      Promise.resolve({ id, title: `${id} full`, entities: [{ id: `${id}-e`, name: 'E' }], interfaces: [], diagrams: [] }),
    );
    markdownMock.exportRefinement.mockReturnValue('# refinement export');
  });

  it('hydrates full architecture designs (alongside knowledge bases) before export', async () => {
    apiMock.getRefinement.mockResolvedValue({
      ...baseRefinement,
      architecture_designs: [{ id: 'arch-1', title: 'Refinement arch', diagrams_count: 1 }] as any,
      knowledge_bases: [{ id: 'kb-1', title: 'KB' }] as any,
    });

    render(<RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Refinement');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    // Architecture summary is hydrated into a full design (entities + diagram payloads).
    await waitFor(() => expect(apiMock.getArchitectureDesign).toHaveBeenCalledWith('arch-1', true));
    // Knowledge bases are still hydrated too (existing behavior preserved).
    expect(apiMock.getRefinementKnowledge).toHaveBeenCalledWith('refinement-1', 'kb-1');

    // exportRefinement receives the hydrated full design, not the summary.
    const lastCall = (markdownMock.exportRefinement.mock.calls.at(-1) ?? []) as any[];
    const arg = lastCall[0];
    expect(arg.architecture_designs[0]).toMatchObject({ id: 'arch-1', entities: [{ id: 'arch-1-e', name: 'E' }] });

    await waitFor(() =>
      expect(markdownMock.downloadMarkdown).toHaveBeenCalledWith('# refinement export', 'refinement_my-refinement_v3.md'),
    );
    expect(apiMock.updateRefinement).not.toHaveBeenCalled();
    expect(apiMock.moveRefinement).not.toHaveBeenCalled();
    expect(apiMock.deleteRefinement).not.toHaveBeenCalled();
  });

  it('exports without architecture calls when the refinement has no architecture designs', async () => {
    render(<RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Refinement');
    fireEvent.click(screen.getByTitle('Download Markdown'));

    await waitFor(() => expect(markdownMock.exportRefinement).toHaveBeenCalled());
    expect(apiMock.getArchitectureDesign).not.toHaveBeenCalled();
    const arg = ((markdownMock.exportRefinement.mock.calls.at(-1) ?? []) as any[])[0];
    expect(arg.architecture_designs).toEqual([]);
  });

  it('renders move actions from the allowed_transitions contract', async () => {
    apiMock.getAllowedTransitions.mockResolvedValueOnce({
      board_id: 'board-1',
      entity_type: 'refinement',
      entity_id: 'refinement-1',
      current_status: 'review',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [
        { to_status: 'draft', label: 'Draft', gate: 'none', blocked_reason: null, blocked_facts: null, preconditions: [], capabilities: [], effects: [], reason_codes: [], policy_compliance: false, policy_compliance_decision: null },
      ],
    });

    render(<RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />);

    await screen.findByText('My Refinement');
    await waitFor(() =>
      expect(apiMock.getAllowedTransitions).toHaveBeenCalledWith('board-1', {
        entity_type: 'refinement',
        entity_id: 'refinement-1',
      }),
    );

    expect(screen.getByRole('button', { name: /Draft/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Approved/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Cancelled/ })).toBeNull();
  });
});

describe('RefinementModal Knowledge tab markdown rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getRefinement.mockResolvedValue({
      ...baseRefinement,
      knowledge_bases: [{ id: 'kb-1', title: 'API Notes' }] as any,
    });
    apiMock.listRefinementSnapshots.mockResolvedValue([]);
    apiMock.listRefinementHistory.mockResolvedValue([]);
    apiMock.listRefinementQA.mockResolvedValue([]);
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'refinement',
      entity_id: 'refinement-1',
      current_status: 'review',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [],
    });
    apiMock.getArchitectureDesign.mockResolvedValue(null);
    const summaryItem = {
      resource_type: 'knowledge_base',
      canonical_unique_resource_id: 'knowledge_base:kb-1',
      versioned_projection_id: 'knowledge_base:kb-1@1',
      root_id: 'kb-1',
      resource_version: '1',
      representative_resource_id: 'kb-1',
      title: 'API Notes',
      attachment_kind: 'direct',
      inherited: false,
      grandfathered: false,
      stale: false,
      superseded: false,
      provenance: {
        source_entity_type: 'refinement',
        source_entity_id: 'refinement-1',
        source_entity_title: 'My Refinement',
        origin_class: 'v2',
        source_revision: '1',
        source_content_sha256: null,
      },
      physical_attachments: [],
      detail_cursor: 'detail-kb-1',
      relevance_links: [],
      body_omitted_reason: 'profile_summary',
    };
    apiMock.getEffectiveResources.mockImplementation(
      async (
        _boardId: string,
        _entityType: string,
        _entityId: string,
        options: { profile: string },
      ) => ({
        board_id: 'board-1',
        entity_type: 'refinement',
        entity_id: 'refinement-1',
        profile: options.profile,
        items: options.profile === 'detail'
          ? [{
            ...summaryItem,
            body: { content: '# KB Heading\n\nThis is **bold** markdown' },
            body_omitted_reason: undefined,
          }]
          : [summaryItem],
        next_cursor: null,
        resources: { architecture: [], mockup: [], knowledge_base: [] },
      }),
    );
  });

  it('renders knowledge base content as markdown elements, not plain text', async () => {
    render(
      <RefinementModal refinementId="refinement-1" boardId="board-1" onClose={vi.fn()} onChanged={vi.fn()} />,
    );

    await screen.findByText('My Refinement');

    fireEvent.click(screen.getByRole('tab', { name: 'Resources' }));
    fireEvent.click(screen.getByRole('tab', { name: /Knowledge/ }));

    // The bounded summary loads, then expanding hydrates one detail projection.
    const kbTitle = await screen.findByText('API Notes');
    expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
      'board-1',
      'refinement',
      'refinement-1',
      { profile: 'summary', limit: 25 },
    );
    fireEvent.click(kbTitle);

    await waitFor(() =>
      expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
        'board-1',
        'refinement',
        'refinement-1',
        { profile: 'detail', cursor: 'detail-kb-1' },
      ),
    );
    expect(apiMock.listRefinementKnowledge).not.toHaveBeenCalled();

    // Markdown becomes real elements: heading + <strong>, no raw text dump.
    expect(await screen.findByRole('heading', { name: 'KB Heading' })).toBeInTheDocument();
    const bold = screen.getByText('bold');
    expect(bold.tagName).toBe('STRONG');
    expect(screen.queryByText('This is **bold** markdown')).toBeNull();
  });
});

describe('RefinementModal selective Knowledge derivation', () => {
  const doneRefinement = {
    ...baseRefinement,
    status: 'done',
    knowledge_bases: [
      {
        id: 'copied-refinement-kb',
        refinement_id: 'refinement-1',
        title: 'Derived Knowledge',
        description: 'Reference for the spec',
        mime_type: 'text/markdown',
        root_source_kb_id: 'root-refinement-kb',
        created_at: '2026-05-06T10:00:00Z',
      },
    ],
  } as Refinement;

  const effectiveRefinementKnowledge = {
    board_id: 'board-1',
    entity_type: 'refinement',
    entity_id: 'refinement-1',
    resources: {
      architecture_design: [],
      screen_mockup: [],
      knowledge_base: [
        {
          id: 'effective-direct-row',
          resource_type: 'knowledge_base',
          resource_id: 'effective-direct-resource',
          attachment_kind: 'direct',
          inherited: false,
          read_only: false,
          hydrated: true,
          ref: {
            root_resource_id: 'root-refinement-kb',
            knowledge_assignment_stale: false,
            origin_class: 'v2',
          },
          resource: {
            id: 'effective-direct-resource',
            title: 'Derived Knowledge',
            description: 'Reference for the spec',
          },
        },
        {
          id: 'effective-inherited-row',
          resource_type: 'knowledge_base',
          resource_id: 'effective-inherited-resource',
          attachment_kind: 'inherited_reference',
          inherited: true,
          read_only: true,
          hydrated: true,
          ref: {
            root_resource_id: 'root-inherited-refinement-kb',
            knowledge_assignment_stale: true,
            origin_class: 'selected_legacy',
          },
          provenance: {
            source_entity_type: 'ideation',
            source_entity_id: 'ideation-1',
            source_entity_title: 'Parent ideation',
          },
          resource: {
            id: 'effective-inherited-resource',
            title: 'Inherited refinement knowledge',
            description: 'Inherited technical reference',
          },
        },
      ],
    },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getRefinement.mockResolvedValue(doneRefinement);
    apiMock.getEffectiveResources.mockResolvedValue(
      effectiveRefinementKnowledge,
    );
    apiMock.listRefinementSnapshots.mockResolvedValue([]);
    apiMock.listRefinementHistory.mockResolvedValue([]);
    apiMock.listRefinementQA.mockResolvedValue([]);
    apiMock.getArchitectureDesign.mockResolvedValue(null);
    apiMock.getAllowedTransitions.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'refinement',
      entity_id: 'refinement-1',
      current_status: 'done',
      source: 'core_sdlc_registry_v1',
      allowed_transitions: [],
    });
    apiMock.deriveSpecFromRefinement.mockResolvedValue({
      contract_version: 2,
      target_type: 'spec',
      target_id: 'spec-derived',
      spec_id: 'spec-derived',
      operation_id: 'op-derive',
      revision: 1,
      replayed: false,
      selection_state: 'explicit_ids',
      assignments: [],
    });
  });

  it('opens a knowledge-only selector with direct and inherited effective inventory', async () => {
    const onChanged = vi.fn();
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    );

    await screen.findByText('My Refinement');
    expect(screen.queryByTestId('context-selector')).toBeNull();

    fireEvent.click(
      screen.getByRole('button', { name: 'Create Spec Draft' }),
    );
    const selector = await screen.findByTestId('context-selector');
    await waitFor(() =>
      expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
        'board-1',
        'refinement',
        'refinement-1',
      ),
    );
    expect(selector).toHaveAttribute('data-knowledge-only', 'true');
    expect(selector).toHaveAttribute('data-context-count', '0');
    expect(screen.getByTestId('selector-description')).not.toHaveTextContent(
      /parts of the refinement|title/i,
    );
    expect(selector).toHaveTextContent('root-refinement-kb');
    expect(selector).toHaveTextContent('root-inherited-refinement-kb');
    expect(selector).toHaveTextContent('Inherited refinement knowledge');
    expect(selector).toHaveTextContent('stale');
    expect(selector).toHaveTextContent('selected_legacy');
    const selectorProps = contextSelectorMock.mock.calls.at(-1)?.[0];
    expect(selectorProps).toEqual(
      expect.objectContaining({
        knowledgeOnly: true,
        items: [],
      }),
    );
    expect(selectorProps.knowledgeItems).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: 'root-inherited-refinement-kb',
          stale: true,
          origin_class: 'selected_legacy',
        }),
      ]),
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Confirm selector' }),
    );

    await waitFor(() =>
      expect(apiMock.deriveSpecFromRefinement).toHaveBeenCalledTimes(1),
    );
    expect(apiMock.deriveSpecFromRefinement).toHaveBeenCalledWith(
      'refinement-1',
      {
        knowledge_propagation: {
          contract_version: 2,
          selection_state: 'explicit_ids',
          mode: 'reference',
          knowledge_ids: ['root-refinement-kb'],
          justification: 'Required by the derived functional scope',
          idempotency_key: expect.any(String),
          expected_revision: 0,
          relevance_links: [],
        },
      },
    );
    const deriveBody =
      apiMock.deriveSpecFromRefinement.mock.calls[0][1];
    expect(Object.keys(deriveBody)).toEqual(['knowledge_propagation']);
    expect(deriveBody).not.toHaveProperty('title');
    expect(deriveBody).not.toHaveProperty('context');
    await waitFor(() =>
      expect(screen.queryByTestId('context-selector')).toBeNull(),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it('keeps the selector open after an error and reuses the same key for an exact retry', async () => {
    apiMock.deriveSpecFromRefinement.mockRejectedValueOnce(
      new Error('derive temporarily unavailable'),
    );
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    fireEvent.click(
      screen.getByRole('button', { name: 'Create Spec Draft' }),
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Confirm selector' }),
    );

    await waitFor(() =>
      expect((toast as any).error).toHaveBeenCalledWith(
        'derive temporarily unavailable',
      ),
    );
    expect(screen.getByTestId('context-selector')).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Confirm selector' }),
      ).toBeEnabled(),
    );

    apiMock.deriveSpecFromRefinement.mockResolvedValueOnce({
      contract_version: 2,
      target_type: 'spec',
      target_id: 'spec-derived',
      spec_id: 'spec-derived',
      operation_id: 'op-derive',
      revision: 1,
      replayed: true,
      selection_state: 'explicit_ids',
      assignments: [],
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Confirm selector' }),
    );

    await waitFor(() =>
      expect(apiMock.deriveSpecFromRefinement).toHaveBeenCalledTimes(2),
    );
    const firstKey =
      apiMock.deriveSpecFromRefinement.mock.calls[0][1]
        .knowledge_propagation.idempotency_key;
    const retryKey =
      apiMock.deriveSpecFromRefinement.mock.calls[1][1]
        .knowledge_propagation.idempotency_key;
    expect(retryKey).toBe(firstKey);
    await waitFor(() =>
      expect(screen.queryByTestId('context-selector')).toBeNull(),
    );
  });

  it('rotates the key when a failed derive changes intent and sends explicit_empty', async () => {
    apiMock.deriveSpecFromRefinement.mockRejectedValueOnce(
      new Error('derive temporarily unavailable'),
    );
    render(
      <RefinementModal
        refinementId="refinement-1"
        boardId="board-1"
        onClose={vi.fn()}
        onChanged={vi.fn()}
      />,
    );

    await screen.findByText('My Refinement');
    fireEvent.click(
      screen.getByRole('button', { name: 'Create Spec Draft' }),
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Confirm selector' }),
    );

    await waitFor(() =>
      expect(apiMock.deriveSpecFromRefinement).toHaveBeenCalledTimes(1),
    );
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Confirm explicit empty' }),
      ).toBeEnabled(),
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Confirm explicit empty' }),
    );

    await waitFor(() =>
      expect(apiMock.deriveSpecFromRefinement).toHaveBeenCalledTimes(2),
    );
    const firstEnvelope =
      apiMock.deriveSpecFromRefinement.mock.calls[0][1]
        .knowledge_propagation;
    const changedEnvelope =
      apiMock.deriveSpecFromRefinement.mock.calls[1][1]
        .knowledge_propagation;
    expect(changedEnvelope.idempotency_key).not.toBe(
      firstEnvelope.idempotency_key,
    );
    expect(changedEnvelope).toMatchObject({
      contract_version: 2,
      selection_state: 'explicit_empty',
      mode: 'drop',
      knowledge_ids: [],
      justification: 'No Knowledge is relevant to the derived spec',
    });
  });
});
