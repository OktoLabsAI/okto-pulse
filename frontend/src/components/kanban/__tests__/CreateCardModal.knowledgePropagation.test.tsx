import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CreateCardModal } from '../CreateCardModal';

const apiMock = vi.hoisted(() => ({
  listAgentsForBoard: vi.fn(),
  listSpecs: vi.fn(),
  getSpec: vi.fn(),
  getEffectiveResources: vi.fn(),
  createCard: vi.fn(),
}));

const storeMock = vi.hoisted(() => ({
  addCardToColumn: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: () => storeMock,
  useColumns: () => ({
    not_started: [],
    started: [],
    in_progress: [],
    validation: [],
    done: [],
    on_hold: [],
    cancelled: [],
  }),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

const specSummary = {
  id: 'spec-1',
  board_id: 'board-1',
  title: 'Selective propagation spec',
  status: 'approved',
  version: 1,
  ideation_id: null,
  refinement_id: null,
  description: null,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-07-23T10:00:00Z',
  updated_at: '2026-07-23T10:00:00Z',
  labels: [],
};

const specWithKnowledge = {
  ...specSummary,
  context: null,
  functional_requirements: [],
  technical_requirements: [],
  acceptance_criteria: [],
  test_scenarios: [],
  business_rules: [],
  api_contracts: [],
  integration_requirements: [],
  observability_requirements: [],
  decisions: [],
  screen_mockups: [],
  architecture_designs: [],
  skip_test_coverage: false,
  cards: [],
  qa_items: [],
  knowledge_bases: [
    {
      id: 'copied-row-1',
      spec_id: 'spec-1',
      title: 'Stable root knowledge',
      description: 'Technical reference',
      mime_type: 'text/markdown',
      root_source_kb_id: 'root-kb-1',
      created_at: '2026-07-23T10:00:00Z',
    },
  ],
};

const effectiveSpecKnowledge = {
  board_id: 'board-1',
  entity_type: 'spec',
  entity_id: 'spec-1',
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
          root_resource_id: 'root-kb-1',
          knowledge_assignment_stale: false,
          origin_class: 'v2',
        },
        resource: {
          id: 'effective-direct-resource',
          title: 'Stable root knowledge',
          description: 'Technical reference',
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
          root_resource_id: 'root-inherited-kb',
          knowledge_assignment_stale: true,
          origin_class: 'legacy_all',
        },
        provenance: {
          source_entity_type: 'refinement',
          source_entity_id: 'refinement-parent',
          source_entity_title: 'Parent refinement',
        },
        resource: {
          id: 'effective-inherited-resource',
          title: 'Inherited stale knowledge',
          description: 'Inherited technical reference',
        },
      },
    ],
  },
};

const createdCard = {
  id: 'card-1',
  board_id: 'board-1',
  spec_id: 'spec-1',
  sprint_id: null,
  title: 'Implement selective propagation',
  description: null,
  details: null,
  status: 'not_started',
  priority: 'none',
  position: 0,
  assignee_id: null,
  created_by: 'agent-1',
  created_at: '2026-07-23T10:00:00Z',
  updated_at: '2026-07-23T10:00:00Z',
  due_date: null,
  labels: [],
  test_scenario_ids: null,
  screen_mockups: [],
  knowledge_bases: [],
  conclusions: [],
  attachments: [],
  qa_items: [],
  comments: [],
  card_type: 'normal',
  origin_task_id: null,
  severity: null,
  linked_test_task_ids: [],
};

function specSelect(): HTMLSelectElement {
  const label = screen.getByText('Spec *');
  const select = label.parentElement?.querySelector('select');
  if (!(select instanceof HTMLSelectElement)) {
    throw new Error('Spec selector not found');
  }
  return select;
}

async function chooseSpec() {
  await screen.findByRole('option', {
    name: 'Selective propagation spec (approved)',
  });
  fireEvent.change(specSelect(), { target: { value: 'spec-1' } });
  await screen.findByRole('checkbox', { name: 'Select Stable root knowledge' });
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.listAgentsForBoard.mockResolvedValue([]);
  apiMock.listSpecs.mockImplementation(
    (_boardId: string, status: string) =>
      Promise.resolve(status === 'approved' ? [specSummary] : []),
  );
  apiMock.getSpec.mockResolvedValue(specWithKnowledge);
  apiMock.getEffectiveResources.mockResolvedValue(effectiveSpecKnowledge);
  apiMock.createCard.mockResolvedValue(createdCard);
});

describe('CreateCardModal selective Knowledge integration', () => {
  it('starts with zero KBs selected and sends an authoritative v2 omitted envelope', async () => {
    const onClose = vi.fn();
    render(
      <CreateCardModal boardId="board-1" initialStatus="not_started" onClose={onClose} />,
    );

    await waitFor(() => expect(apiMock.listSpecs).toHaveBeenCalledTimes(4));
    await chooseSpec();

    const knowledge = screen.getByRole('checkbox', {
      name: 'Select Stable root knowledge',
    });
    expect(knowledge).not.toBeChecked();
    expect(knowledge).toBeDisabled();
    expect(screen.getByText('No resource starts selected. Omitted never means “select all”.')).toBeInTheDocument();

    fireEvent.change(
      screen.getByPlaceholderText('E.g.: Implement feature X'),
      { target: { value: 'Implement selective propagation' } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

    await waitFor(() => expect(apiMock.createCard).toHaveBeenCalledTimes(1));
    expect(apiMock.createCard).toHaveBeenCalledWith(
      'board-1',
      expect.objectContaining({
        title: 'Implement selective propagation',
        spec_id: 'spec-1',
        knowledge_propagation: {
          contract_version: 2,
          selection_state: 'omitted',
          mode: null,
          knowledge_ids: [],
          justification: null,
          idempotency_key: expect.any(String),
          expected_revision: 0,
          relevance_links: [],
        },
      }),
    );
    expect(storeMock.addCardToColumn).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'card-1', spec_id: 'spec-1' }),
    );
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('uses the effective inventory, including inherited provenance, and sends its stable root', async () => {
    render(
      <CreateCardModal
        boardId="board-1"
        initialStatus="not_started"
        onClose={vi.fn()}
      />,
    );

    await chooseSpec();
    await waitFor(() =>
      expect(apiMock.getEffectiveResources).toHaveBeenCalledWith(
        'board-1',
        'spec',
        'spec-1',
      ),
    );
    expect(
      screen.getByRole('checkbox', {
        name: 'Select Inherited stale knowledge',
      }),
    ).toBeInTheDocument();
    expect(screen.getByText('stale')).toBeInTheDocument();
    expect(screen.getByText('legacy all')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('radio', { name: 'Reference' }));
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: 'Select Inherited stale knowledge',
      }),
    );
    fireEvent.change(
      screen.getByLabelText('Relevance justification *'),
      { target: { value: 'Required by FR-B1 and AC-B17' } },
    );
    fireEvent.change(
      screen.getByPlaceholderText('E.g.: Implement feature X'),
      { target: { value: 'Implement governed selector' } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

    await waitFor(() => expect(apiMock.createCard).toHaveBeenCalledTimes(1));
    const request = apiMock.createCard.mock.calls[0][1];
    expect(request.knowledge_propagation).toMatchObject({
      contract_version: 2,
      selection_state: 'explicit_ids',
      mode: 'reference',
      knowledge_ids: ['root-inherited-kb'],
      justification: 'Required by FR-B1 and AC-B17',
      expected_revision: 0,
    });
    expect(request.knowledge_propagation.knowledge_ids).not.toContain(
      'effective-inherited-row',
    );
  });

  it('preserves the idempotency key for an exact retry after failure', async () => {
    apiMock.createCard.mockRejectedValueOnce(
      new Error('create temporarily unavailable'),
    );
    render(
      <CreateCardModal
        boardId="board-1"
        initialStatus="not_started"
        onClose={vi.fn()}
      />,
    );

    await chooseSpec();
    fireEvent.change(
      screen.getByPlaceholderText('E.g.: Implement feature X'),
      { target: { value: 'Retry the exact request' } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

    await waitFor(() => expect(apiMock.createCard).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Create Card' }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

    await waitFor(() => expect(apiMock.createCard).toHaveBeenCalledTimes(2));
    const firstKey =
      apiMock.createCard.mock.calls[0][1]
        .knowledge_propagation.idempotency_key;
    const retryKey =
      apiMock.createCard.mock.calls[1][1]
        .knowledge_propagation.idempotency_key;
    expect(retryKey).toBe(firstKey);
  });

  it('rotates the key when a failed request changes intent and sends explicit_empty', async () => {
    apiMock.createCard.mockRejectedValueOnce(
      new Error('create temporarily unavailable'),
    );
    render(
      <CreateCardModal
        boardId="board-1"
        initialStatus="not_started"
        onClose={vi.fn()}
      />,
    );

    await chooseSpec();
    const titleInput = screen.getByPlaceholderText(
      'E.g.: Implement feature X',
    );
    fireEvent.change(titleInput, {
      target: { value: 'Initial omitted request' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

    await waitFor(() => expect(apiMock.createCard).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Create Card' }),
      ).toBeEnabled(),
    );
    fireEvent.change(titleInput, {
      target: { value: 'Changed explicit-empty request' },
    });
    fireEvent.click(screen.getByRole('radio', { name: 'Drop' }));
    fireEvent.change(
      screen.getByLabelText('Relevance justification *'),
      { target: { value: 'No Knowledge is relevant to this card' } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));

    await waitFor(() => expect(apiMock.createCard).toHaveBeenCalledTimes(2));
    const firstEnvelope =
      apiMock.createCard.mock.calls[0][1].knowledge_propagation;
    const changedEnvelope =
      apiMock.createCard.mock.calls[1][1].knowledge_propagation;
    expect(changedEnvelope.idempotency_key).not.toBe(
      firstEnvelope.idempotency_key,
    );
    expect(changedEnvelope).toMatchObject({
      contract_version: 2,
      selection_state: 'explicit_empty',
      mode: 'drop',
      knowledge_ids: [],
      justification: 'No Knowledge is relevant to this card',
    });
  });

  it('consumes Escape and ignores backdrop clicks while create is in flight', async () => {
    let resolveCreate!: (value: typeof createdCard) => void;
    apiMock.createCard.mockImplementation(
      () =>
        new Promise<typeof createdCard>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const onClose = vi.fn();
    const { container } = render(
      <CreateCardModal boardId="board-1" initialStatus="not_started" onClose={onClose} />,
    );

    await chooseSpec();
    fireEvent.change(
      screen.getByPlaceholderText('E.g.: Implement feature X'),
      { target: { value: 'Guard busy close paths' } },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Create Card' }));
    await screen.findByRole('button', { name: 'Creating...' });

    fireEvent.keyDown(document, { key: 'Escape' });
    const overlay = container.querySelector('.modal-overlay');
    if (!(overlay instanceof HTMLElement)) {
      throw new Error('Modal overlay not found');
    }
    fireEvent.click(overlay);
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => {
      resolveCreate(createdCard);
      await Promise.resolve();
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
