import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CardKnowledgeTab } from '../CardKnowledgeTab';
import { AuthenticatedFetchError } from '@/lib/authFetch';

const apiMock = vi.hoisted(() => ({
  getEffectiveResources: vi.fn(),
  getCardKnowledgeAssignments: vi.fn(),
  replaceCardKnowledgeAssignments: vi.fn(),
  dropCardKnowledgeAssignments: vi.fn(),
  refreshCardKnowledgeAssignments: vi.fn(),
  markResourceNotApplicable: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('react-hot-toast', () => ({
  default: toastMock,
}));

const baseCard = {
  id: 'c1',
  board_id: 'b1',
  spec_id: 's1',
  title: 'Card under test',
  description: null,
  status: 'in_progress',
  priority: 'none',
  position: 0,
  card_type: 'normal',
  knowledge_bases: [
    {
      id: 'kb_existing',
      title: 'Existing KB',
      description: 'desc',
      content: 'orig content',
      mime_type: 'text/markdown',
      source: 'copied_from_spec:s1:sk_1',
      source_kb_id: 'sk_1',
    },
  ],
} as any;

const emptyEffectiveResources = {
  resources: {
    architecture: [],
    mockup: [],
    knowledge_base: [],
  },
};

const emptyTechnicalRead = {
  contract_version: 2 as const,
  revision: 7,
  selection_state: 'omitted' as const,
  assignments: [],
};

const mutationResponse = {
  contract_version: 2 as const,
  target_type: 'card' as const,
  target_id: 'c1',
  operation_id: 'operation-1',
  revision: 8,
  replayed: false,
  selection_state: 'explicit_ids' as const,
  assignments: [],
};

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function renderTab({
  card = baseCard,
  specKnowledgeBases = [],
  onUpdate = vi.fn().mockResolvedValue(undefined),
  onBusyChange = vi.fn(),
  readOnly = false,
}: {
  card?: any;
  specKnowledgeBases?: any[];
  onUpdate?: () => Promise<void>;
  onBusyChange?: (busy: boolean) => void;
  readOnly?: boolean;
} = {}) {
  return render(
    <CardKnowledgeTab
      card={card}
      specKnowledgeBases={specKnowledgeBases}
      onUpdate={onUpdate}
      onBusyChange={onBusyChange}
      readOnly={readOnly}
    />,
  );
}

beforeEach(() => {
  document.body.innerHTML = '';
  vi.clearAllMocks();
  apiMock.getEffectiveResources.mockResolvedValue(emptyEffectiveResources);
  apiMock.getCardKnowledgeAssignments.mockResolvedValue(emptyTechnicalRead);
  apiMock.replaceCardKnowledgeAssignments.mockResolvedValue(mutationResponse);
  apiMock.dropCardKnowledgeAssignments.mockResolvedValue({
    ...mutationResponse,
    selection_state: 'explicit_empty',
  });
  apiMock.refreshCardKnowledgeAssignments.mockResolvedValue({
    contract_version: 2,
    operation_id: 'refresh-1',
    revision: 8,
    replayed: false,
    refreshed: [],
  });
});

describe('CardKnowledgeTab', () => {
  it('keeps existing Knowledge content read-only and expandable', async () => {
    renderTab();

    expect(
      await screen.findByText(
        'Knowledge content is read-only; propagation decisions are governed below.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('Existing KB')).toBeInTheDocument();
    expect(screen.getByText('from spec')).toBeInTheDocument();
    expect(screen.queryByText(/New KB/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('kb-edit-kb_existing')).not.toBeInTheDocument();
    expect(screen.queryByTestId('kb-delete-kb_existing')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('kb-row-kb_existing'));
    expect(screen.getByText('orig content')).toBeInTheDocument();
  });

  it('hides governed propagation mutations while a Rejected card is frozen', async () => {
    apiMock.getCardKnowledgeAssignments.mockResolvedValue({
      ...emptyTechnicalRead,
      assignments: [{
        root_knowledge_id: 'kb_existing',
        mode: 'snapshot',
        origin_class: 'v2',
        stale: true,
      }],
    });

    renderTab({ readOnly: true });

    expect(await screen.findByText('Current governed selection')).toBeInTheDocument();
    expect(screen.getByText(/Knowledge is read-only while this card is Rejected/))
      .toBeInTheDocument();
    expect(within(screen.getByTestId('knowledge-assignment-kb_existing'))
      .queryByRole('button', { name: 'Refresh' }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save explicit decision' }))
      .not.toBeInTheDocument();
    expect(screen.queryByTestId('card-knowledge-propagation'))
      .not.toBeInTheDocument();
  });

  it('downloads the existing snapshot as Markdown', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:mock-url');
    const revokeObjectURL = vi.fn();
    (URL as any).createObjectURL = createObjectURL;
    (URL as any).revokeObjectURL = revokeObjectURL;

    renderTab();
    fireEvent.click(await screen.findByTestId('kb-download-kb_existing'));

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url');
  });

  it('renders an honest empty state after both governed reads finish', async () => {
    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
    });

    expect(await screen.findByText('No knowledge bases')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Choose an explicit reference or snapshot below when one is relevant.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('No active governed assignments.')).toBeInTheDocument();
  });

  it('renders inherited effective Knowledge and deduplicates a copied snapshot', async () => {
    apiMock.getEffectiveResources.mockResolvedValue({
      resources: {
        architecture: [],
        mockup: [],
        knowledge_base: [
          {
            id: 'sk_1',
            title: 'Existing KB',
            resource_type: 'knowledge_base',
            attachment_kind: 'inherited_reference',
            inherited: true,
            read_only: true,
            hydrated: true,
            source_entity_type: 'spec',
            source_entity_id: 's1',
            source_entity_title: 'Parent spec',
            resource: {
              id: 'sk_1',
              title: 'Existing KB',
              content: 'parent content',
              mime_type: 'text/markdown',
            },
          },
          {
            id: 'kb_parent',
            title: 'Parent KB',
            resource_type: 'knowledge_base',
            attachment_kind: 'inherited_reference',
            inherited: true,
            read_only: true,
            hydrated: true,
            source_entity_type: 'spec',
            source_entity_id: 's1',
            source_entity_title: 'Parent spec',
            resource: {
              id: 'kb_parent',
              title: 'Parent KB',
              content: 'parent content',
              mime_type: 'text/markdown',
            },
          },
        ],
      },
    });

    renderTab();

    const inheritedRow = await screen.findByTestId('kb-row-kb_parent');
    expect(within(inheritedRow).getByText('Parent KB')).toBeInTheDocument();
    expect(screen.getAllByTestId('kb-row-kb_existing')).toHaveLength(1);
    expect(within(inheritedRow).getByText('from spec: Parent spec')).toBeInTheDocument();
  });

  it('shows revision, mode, stale state and origin metadata from the technical read', async () => {
    apiMock.getCardKnowledgeAssignments.mockResolvedValue({
      contract_version: 2,
      revision: 11,
      selection_state: 'explicit_ids',
      assignments: [
        {
          root_knowledge_id: 'root-snapshot',
          mode: 'snapshot',
          origin_class: 'selected_legacy',
          state: 'stale',
          stale: true,
        },
        {
          root_knowledge_id: 'root-reference',
          mode: 'reference',
          origin_class: 'legacy_unresolved',
          state: 'active',
          stale: false,
        },
      ],
    });

    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      specKnowledgeBases: [
        {
          id: 'local-snapshot',
          root_source_kb_id: 'root-snapshot',
          title: 'Snapshot notes',
          content: '',
        },
        {
          id: 'local-reference',
          root_source_kb_id: 'root-reference',
          title: 'Reference notes',
          content: '',
        },
      ],
    });

    expect(await screen.findByText('revision 11')).toBeInTheDocument();
    expect(screen.getByText('explicit_ids')).toBeInTheDocument();

    const snapshot = screen.getByTestId('knowledge-assignment-root-snapshot');
    expect(within(snapshot).getByText('Snapshot notes')).toBeInTheDocument();
    expect(within(snapshot).getByText('snapshot')).toBeInTheDocument();
    expect(within(snapshot).getByText('selected legacy')).toBeInTheDocument();
    expect(within(snapshot).getByText('stale')).toBeInTheDocument();
    expect(within(snapshot).getByRole('button', { name: 'Refresh' })).toBeInTheDocument();

    const reference = screen.getByTestId('knowledge-assignment-root-reference');
    expect(within(reference).getByText('legacy unresolved')).toBeInTheDocument();
    expect(within(reference).queryByRole('button', { name: 'Refresh' })).not.toBeInTheDocument();
  });

  it('builds the selectable source inventory from effective spec roots and ref metadata', async () => {
    apiMock.getEffectiveResources.mockImplementation(
      (
        _boardId: string,
        entityType: string,
        _entityId: string,
        options?: { profile?: string; cursor?: string; limit?: number },
      ) => {
        if (entityType !== 'spec') return Promise.resolve(emptyEffectiveResources);
        return Promise.resolve({
          board_id: 'b1',
          entity_type: 'spec',
          entity_id: 's1',
          profile: 'summary',
          items: [{
            resource_type: 'knowledge_base',
            canonical_unique_resource_id: 'knowledge_base:root-source-id',
            versioned_projection_id: 'knowledge_base:root-source-id@7',
            root_id: 'root-source-id',
            resource_version: '7',
            representative_resource_id: 'physical-child-id',
            title: 'Inherited source reference',
            attachment_kind: 'inherited_reference',
            inherited: true,
            grandfathered: false,
            stale: true,
            superseded: false,
            provenance: {
              source_entity_type: 'spec',
              source_entity_id: 's1',
              source_entity_title: 'Parent spec',
              origin_class: 'selected_legacy',
              source_revision: '7',
              source_content_sha256: null,
            },
            physical_attachments: [],
            detail_cursor: 'detail-source',
            relevance_links: [],
            body_omitted_reason: 'profile_summary',
          }],
          next_cursor: null,
          resources: {
            architecture: [],
            mockup: [],
            knowledge_base: [],
          },
          request_options: options,
        });
      },
    );

    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      specKnowledgeBases: [],
    });

    await waitFor(() => {
      expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
        'b1',
        'spec',
        's1',
        { profile: 'summary', limit: 25 },
      );
    });
    const selector = await screen.findByTestId('card-knowledge-propagation');
    expect(within(selector).getByText('Inherited source reference')).toBeInTheDocument();
    expect(within(selector).getByText('stale')).toBeInTheDocument();
    expect(within(selector).getByText('selected legacy')).toBeInTheDocument();

    fireEvent.click(within(selector).getByRole('radio', { name: 'Reference' }));
    fireEvent.click(
      within(selector).getByRole('checkbox', {
        name: 'Select Inherited source reference',
      }),
    );
    fireEvent.change(within(selector).getByLabelText(/Relevance justification/i), {
      target: { value: 'The effective inherited root is relevant.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save explicit decision' }));

    await waitFor(() => {
      expect(apiMock.replaceCardKnowledgeAssignments).toHaveBeenCalledWith(
        'c1',
        expect.objectContaining({
          knowledge_ids: ['root-source-id'],
          expected_revision: 7,
        }),
      );
    });
  });

  it('paginates the source spec inventory with summary projections only', async () => {
    const summaryItem = (rootId: string, title: string) => ({
      resource_type: 'knowledge_base',
      canonical_unique_resource_id: `knowledge_base:${rootId}`,
      versioned_projection_id: `knowledge_base:${rootId}@1`,
      root_id: rootId,
      resource_version: '1',
      representative_resource_id: `${rootId}-physical`,
      title,
      inherited: false,
      grandfathered: false,
      stale: false,
      superseded: false,
      provenance: {
        source_entity_type: 'spec',
        source_entity_id: 's1',
        source_entity_title: 'Parent spec',
        origin_class: 'v2',
        source_revision: '1',
        source_content_sha256: null,
      },
      physical_attachments: [],
      detail_cursor: `detail-${rootId}`,
      relevance_links: [],
      body_omitted_reason: 'profile_summary',
    });
    apiMock.getEffectiveResources.mockImplementation(
      (
        _boardId: string,
        entityType: string,
        _entityId: string,
        options?: { cursor?: string },
      ) => {
        if (entityType !== 'spec') return Promise.resolve(emptyEffectiveResources);
        return Promise.resolve({
          board_id: 'b1',
          entity_type: 'spec',
          entity_id: 's1',
          profile: 'summary',
          items: options?.cursor
            ? [summaryItem('root-page-2', 'Page two KB')]
            : [summaryItem('root-page-1', 'Page one KB')],
          next_cursor: options?.cursor ? null : 'source-page-2',
          resources: { architecture: [], mockup: [], knowledge_base: [] },
        });
      },
    );

    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      specKnowledgeBases: [],
    });

    const selector = await screen.findByTestId('card-knowledge-propagation');
    expect(within(selector).getByText('Page one KB')).toBeInTheDocument();
    expect(within(selector).getByText('Page two KB')).toBeInTheDocument();
    expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
      'b1',
      'spec',
      's1',
      { profile: 'summary', limit: 25 },
    );
    expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
      'b1',
      'spec',
      's1',
      { profile: 'summary', limit: 25, cursor: 'source-page-2' },
    );
  });

  it('assigns selected stable roots with the current revision', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      onUpdate,
      specKnowledgeBases: [
        {
          id: 'local-spec-kb',
          root_source_kb_id: 'root-spec-kb',
          title: 'Spec KB',
          description: 'Relevant implementation notes',
          content: 'body',
        },
      ],
    });

    await screen.findByText('revision 7');
    fireEvent.click(screen.getByRole('radio', { name: 'Reference' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Spec KB' }));
    fireEvent.change(screen.getByLabelText(/Relevance justification/i), {
      target: { value: 'Required to implement the linked acceptance criterion.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save explicit decision' }));

    await waitFor(() => {
      expect(apiMock.replaceCardKnowledgeAssignments).toHaveBeenCalledWith(
        'c1',
        {
          contract_version: 2,
          knowledge_ids: ['root-spec-kb'],
          mode: 'reference',
          justification: 'Required to implement the linked acceptance criterion.',
          idempotency_key: expect.any(String),
          expected_revision: 7,
          linkage: [],
        },
      );
    });
    expect(apiMock.dropCardKnowledgeAssignments).not.toHaveBeenCalled();
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(toastMock.success).toHaveBeenCalledWith('Knowledge assignments saved');
  });

  it('preserves an explicit-empty DROP separately from Resource Gate N/A', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      onUpdate,
    });

    await screen.findByText('revision 7');
    fireEvent.click(screen.getByRole('radio', { name: 'Drop' }));
    expect(screen.getByText(/Explicit empty will be saved/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Relevance justification/i), {
      target: { value: 'No inherited Knowledge applies to this card.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save explicit decision' }));

    await waitFor(() => {
      expect(apiMock.dropCardKnowledgeAssignments).toHaveBeenCalledWith(
        'c1',
        {
          contract_version: 2,
          knowledge_ids: [],
          justification: 'No inherited Knowledge applies to this card.',
          idempotency_key: expect.any(String),
          expected_revision: 7,
        },
      );
    });
    expect(apiMock.replaceCardKnowledgeAssignments).not.toHaveBeenCalled();
    expect(apiMock.markResourceNotApplicable).not.toHaveBeenCalled();
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(
      screen.getByText('DROP and explicit empty do not mark Resource Gate as N/A.'),
    ).toBeInTheDocument();
  });

  it('refreshes only a stale snapshot by stable root ID and current revision', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    apiMock.getCardKnowledgeAssignments.mockResolvedValue({
      contract_version: 2,
      revision: 13,
      selection_state: 'explicit_ids',
      assignments: [
        {
          root_knowledge_id: 'root-stale',
          mode: 'snapshot',
          origin_class: 'v2',
          state: 'stale',
          stale: true,
        },
      ],
    });

    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      onUpdate,
      specKnowledgeBases: [
        {
          id: 'local-stale',
          root_source_kb_id: 'root-stale',
          title: 'Stale snapshot',
          content: '',
        },
      ],
    });

    const assignment = await screen.findByTestId(
      'knowledge-assignment-root-stale',
    );
    fireEvent.click(within(assignment).getByRole('button', { name: 'Refresh' }));

    await waitFor(() => {
      expect(apiMock.refreshCardKnowledgeAssignments).toHaveBeenCalledWith(
        'c1',
        {
          contract_version: 2,
          knowledge_ids: ['root-stale'],
          idempotency_key: expect.any(String),
          expected_revision: 13,
        },
      );
    });
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(toastMock.success).toHaveBeenCalledWith('Knowledge snapshot refreshed');
  });

  it('reuses the refresh key for an exact retry and rotates it after a successful new revision', async () => {
    const staleAt = (revision: number) => ({
      contract_version: 2,
      revision,
      selection_state: 'explicit_ids',
      assignments: [
        {
          root_knowledge_id: 'root-stale',
          mode: 'snapshot',
          origin_class: 'v2',
          state: 'stale',
          stale: true,
        },
      ],
    });
    apiMock.getCardKnowledgeAssignments
      .mockResolvedValueOnce(staleAt(13))
      .mockResolvedValueOnce(staleAt(13))
      .mockResolvedValueOnce(staleAt(14))
      .mockResolvedValue(staleAt(14));
    apiMock.refreshCardKnowledgeAssignments
      .mockRejectedValueOnce(new Error('temporary transport failure'))
      .mockResolvedValue({
        contract_version: 2,
        operation_id: 'refresh-success',
        revision: 14,
        replayed: false,
        refreshed: [],
      });

    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      specKnowledgeBases: [
        {
          id: 'local-stale',
          root_source_kb_id: 'root-stale',
          title: 'Stale snapshot',
          content: '',
        },
      ],
    });

    let refresh = within(
      await screen.findByTestId('knowledge-assignment-root-stale'),
    ).getByRole('button', { name: 'Refresh' });
    fireEvent.click(refresh);
    await waitFor(() => {
      expect(apiMock.refreshCardKnowledgeAssignments).toHaveBeenCalledTimes(1);
      expect(apiMock.getCardKnowledgeAssignments.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    await waitFor(() => expect(refresh).toBeEnabled());

    refresh = within(
      screen.getByTestId('knowledge-assignment-root-stale'),
    ).getByRole('button', { name: 'Refresh' });
    fireEvent.click(refresh);
    await waitFor(() => {
      expect(apiMock.refreshCardKnowledgeAssignments).toHaveBeenCalledTimes(2);
    });
    const firstPayload = apiMock.refreshCardKnowledgeAssignments.mock.calls[0][1];
    const exactRetryPayload = apiMock.refreshCardKnowledgeAssignments.mock.calls[1][1];
    expect(exactRetryPayload.idempotency_key).toBe(firstPayload.idempotency_key);

    await screen.findByText('revision 14');
    refresh = within(
      screen.getByTestId('knowledge-assignment-root-stale'),
    ).getByRole('button', { name: 'Refresh' });
    fireEvent.click(refresh);
    await waitFor(() => {
      expect(apiMock.refreshCardKnowledgeAssignments).toHaveBeenCalledTimes(3);
    });
    const newRevisionPayload = apiMock.refreshCardKnowledgeAssignments.mock.calls[2][1];
    expect(newRevisionPayload.expected_revision).toBe(14);
    expect(newRevisionPayload.idempotency_key).not.toBe(firstPayload.idempotency_key);
  });

  it.each([
    ['assign', 'Reference'],
    ['drop', 'Drop'],
    ['refresh', 'Refresh'],
  ] as const)(
    'reports busy for the complete %s operation lifetime',
    async (operation, controlLabel) => {
      const onBusyChange = vi.fn();
      const pending = deferred<unknown>();

      if (operation === 'refresh') {
        apiMock.getCardKnowledgeAssignments.mockResolvedValue({
          contract_version: 2,
          revision: 13,
          selection_state: 'explicit_ids',
          assignments: [
            {
              root_knowledge_id: 'root-stale',
              mode: 'snapshot',
              origin_class: 'v2',
              state: 'stale',
              stale: true,
            },
          ],
        });
        apiMock.refreshCardKnowledgeAssignments.mockReturnValue(pending.promise);
      } else if (operation === 'assign') {
        apiMock.replaceCardKnowledgeAssignments.mockReturnValue(pending.promise);
      } else {
        apiMock.dropCardKnowledgeAssignments.mockReturnValue(pending.promise);
      }

      renderTab({
        card: { ...baseCard, knowledge_bases: [] },
        specKnowledgeBases: [
          {
            id: 'local-source',
            root_source_kb_id: 'root-source',
            title: 'Source KB',
            content: '',
          },
        ],
        onBusyChange,
      });

      await screen.findByText(operation === 'refresh' ? 'revision 13' : 'revision 7');
      if (operation === 'refresh') {
        const assignment = screen.getByTestId('knowledge-assignment-root-stale');
        fireEvent.click(within(assignment).getByRole('button', { name: controlLabel }));
      } else {
        fireEvent.click(screen.getByRole('radio', { name: controlLabel }));
        if (operation === 'assign') {
          fireEvent.click(screen.getByRole('checkbox', { name: 'Select Source KB' }));
        }
        fireEvent.change(screen.getByLabelText(/Relevance justification/i), {
          target: { value: `Justification for ${operation}.` },
        });
        fireEvent.click(screen.getByRole('button', { name: 'Save explicit decision' }));
      }

      await waitFor(() => {
        expect(onBusyChange).toHaveBeenCalledWith(true);
      });
      expect(onBusyChange).toHaveBeenLastCalledWith(true);

      pending.resolve(
        operation === 'refresh'
          ? {
            contract_version: 2,
            operation_id: 'refresh-settled',
            revision: 14,
            replayed: false,
            refreshed: [],
          }
          : mutationResponse,
      );
      await waitFor(() => {
        expect(onBusyChange).toHaveBeenLastCalledWith(false);
      });
    },
  );

  it('reloads the technical revision after a conflict instead of retrying the stale write', async () => {
    apiMock.replaceCardKnowledgeAssignments.mockRejectedValue(
      new Error('knowledge_propagation_revision_conflict'),
    );

    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      specKnowledgeBases: [
        {
          id: 'local-spec-kb',
          root_source_kb_id: 'root-spec-kb',
          title: 'Spec KB',
          content: '',
        },
      ],
    });

    await screen.findByText('revision 7');
    fireEvent.click(screen.getByRole('radio', { name: 'Reference' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Spec KB' }));
    fireEvent.change(screen.getByLabelText(/Relevance justification/i), {
      target: { value: 'Relevant to this task.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save explicit decision' }));

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'knowledge_propagation_revision_conflict',
      );
      expect(apiMock.getCardKnowledgeAssignments.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    expect(apiMock.replaceCardKnowledgeAssignments).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('radio', { name: 'No decision' })).toBeChecked();
  });

  it('uses typed HTTP 409 metadata to reload the revision and require reconfirmation', async () => {
    const conflict = new AuthenticatedFetchError({
      message: 'The source changed after preflight',
      status: 409,
      code: 'knowledge_propagation_preflight_stale',
      details: {
        status: 409,
        expected_revision: 7,
      },
    });
    apiMock.replaceCardKnowledgeAssignments.mockRejectedValue(conflict);

    renderTab({
      card: { ...baseCard, knowledge_bases: [] },
      specKnowledgeBases: [
        {
          id: 'local-spec-kb',
          root_source_kb_id: 'root-spec-kb',
          title: 'Spec KB',
          content: '',
        },
      ],
    });

    await screen.findByText('revision 7');
    fireEvent.click(screen.getByRole('radio', { name: 'Reference' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Spec KB' }));
    fireEvent.change(screen.getByLabelText(/Relevance justification/i), {
      target: { value: 'Relevant before the source changed.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save explicit decision' }));

    await waitFor(() => {
      expect(apiMock.getCardKnowledgeAssignments.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    expect(apiMock.replaceCardKnowledgeAssignments).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('radio', { name: 'No decision' })).toBeChecked();
  });
});
