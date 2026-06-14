import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GlobalSearchView } from '../GlobalSearchView';
import * as discoveryApi from '@/services/discovery-api';
import * as kgApi from '@/services/kg-api';
import type {
  DiscoveryIntent,
  DiscoverySelectorOptionsResponse,
} from '@/types/discovery';

const mocks = vi.hoisted(() => ({
  pushModal: vi.fn(),
  openCardModal: vi.fn(),
}));

vi.mock('@/services/discovery-api', () => ({
  listIntents: vi.fn(),
  listSelectorOptions: vi.fn(),
  executeIntent: vi.fn(),
}));

vi.mock('@/services/kg-api', () => ({
  globalSearch: vi.fn(),
}));

vi.mock('@/contexts/ModalStackContext', () => ({
  useModalStack: () => ({ push: mocks.pushModal }),
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: (
    selector: (state: { openCardModal: typeof mocks.openCardModal }) => unknown,
  ) => selector({ openCardModal: mocks.openCardModal }),
}));

vi.mock('../NodeDetailModal', () => ({
  NodeDetailModal: () => null,
}));

const BOARD = 'board-discovery';

function intent(
  params_schema: DiscoveryIntent['params_schema'],
): DiscoveryIntent {
  return {
    id: 'intent-1',
    name: 'trace_spec_child',
    label: 'Trace a spec child',
    description: 'Find rows for a structured child',
    category: 'coverage_tracing',
    tool_binding: 'okto_pulse_list_uncovered_requirements',
    params_schema,
    renderer: 'table',
    min_permission: null,
    active: true,
    is_seed: true,
    created_at: '2026-05-27T00:00:00Z',
    updated_at: '2026-05-27T00:00:00Z',
  };
}

function selectorResponse(
  options: DiscoverySelectorOptionsResponse['options'],
): DiscoverySelectorOptionsResponse {
  return {
    options,
    source: 'board_db_spec_json',
    cache_status: 'miss',
    global_refs_used: false,
  };
}

function mockSelectorOptions(childOptions = selectorResponse([])) {
  vi.mocked(discoveryApi.listSelectorOptions).mockImplementation((_board, params) => {
    if (params.selectorKind === 'spec') {
      return Promise.resolve(
        selectorResponse([
          {
            id: 'spec-a',
            label: 'Spec A',
            entity_type: 'spec',
            spec_id: 'spec-a',
            status: 'active',
          },
          {
            id: 'spec-b',
            label: 'Spec B',
            entity_type: 'spec',
            spec_id: 'spec-b',
            status: 'active',
          },
        ]),
      );
    }
    if (params.selectorKind === 'card') {
      return Promise.resolve(
        selectorResponse([
          {
            id: 'card-a',
            label: 'Card A',
            entity_type: 'card',
            status: 'in_progress',
            refs: { card_id: 'card-a' },
          },
        ]),
      );
    }
    return Promise.resolve(childOptions);
  });
}

async function openParamsForm(testIntent: DiscoveryIntent) {
  vi.mocked(discoveryApi.listIntents).mockResolvedValue([testIntent]);
  render(<GlobalSearchView boardId={BOARD} />);
  const intentCard = await screen.findByTestId(
    `discovery-intent-${testIntent.name}`,
  );
  fireEvent.click(intentCard);
  await screen.findByTestId('discovery-params-form');
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.requestAnimationFrame = (callback: FrameRequestCallback) => {
    callback(0);
    return 0;
  };
  vi.mocked(discoveryApi.executeIntent).mockResolvedValue({
    rows: [],
    columns: [],
    total: 0,
    tool_binding: 'okto_pulse_list_uncovered_requirements',
    params_echo: {},
    execution: 'real_tool',
    intent_id: 'intent-1',
    intent_name: 'Trace a spec child',
  });
});

describe('GlobalSearchView typed Discovery params', () => {
  it('sends the selected graph layer to free-text global search', async () => {
    vi.mocked(discoveryApi.listIntents).mockResolvedValue([]);
    vi.mocked(kgApi.globalSearch).mockResolvedValue({
      results: [],
      total: 0,
      graph_layer: 'working',
    });

    render(<GlobalSearchView boardId={BOARD} />);

    fireEvent.click(screen.getByTestId('discovery-graph-layer-working'));
    fireEvent.change(screen.getByTestId('discovery-search-input'), {
      target: { value: 'canonical debt' },
    });
    fireEvent.click(screen.getByTestId('discovery-search-submit'));

    await waitFor(() =>
      expect(kgApi.globalSearch).toHaveBeenCalledWith(
        'canonical debt',
        20,
        0.3,
        'working',
      ),
    );
  });

  it('preserves legacy text params and sends text values unchanged', async () => {
    await openParamsForm(
      intent({
        topic: { required: true, label: 'Topic' },
      }),
    );

    fireEvent.change(screen.getByTestId('discovery-param-topic'), {
      target: { value: 'authorization' },
    });
    fireEvent.click(screen.getByTestId('discovery-params-run'));

    await waitFor(() =>
      expect(discoveryApi.executeIntent).toHaveBeenCalledWith(
        'intent-1',
        BOARD,
        { topic: 'authorization' },
      ),
    );
  });

  it('renders card entity selectors and sends the selected card reference', async () => {
    mockSelectorOptions();

    await openParamsForm(
      intent({
        card_id: {
          type: 'entity_selector',
          entity_type: 'card',
          required: true,
          label: 'Card',
        },
      }),
    );

    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-card_id-spec')).toBeEnabled(),
    );
    expect(discoveryApi.listSelectorOptions).toHaveBeenCalledWith(BOARD, {
      selectorKind: 'card',
      q: null,
      limit: 50,
    });
    fireEvent.change(screen.getByTestId('discovery-selector-card_id-spec'), {
      target: { value: 'card-a' },
    });
    fireEvent.click(screen.getByTestId('discovery-params-run'));

    await waitFor(() =>
      expect(discoveryApi.executeIntent).toHaveBeenCalledWith(
        'intent-1',
        BOARD,
        {
          card_id: {
            card_id: 'card-a',
            entity_id: 'card-a',
            id: 'card-a',
          },
        },
      ),
    );
  });

  it('renders dependent spec child selectors and sends canonical selector payloads', async () => {
    mockSelectorOptions(
      selectorResponse([
        {
          id: 'spec:spec-a:technical_requirement:tr-1',
          label: 'TR-1 Cache invalidation',
          entity_type: 'spec_child',
          spec_id: 'spec-a',
          spec_title: 'Spec A',
          child_type: 'technical_requirement',
          child_id: 'tr-1',
          child_ref: 'spec:spec-a:technical_requirement:tr-1',
          status: 'active',
        },
      ]),
    );

    await openParamsForm(
      intent({
        target: {
          type: 'spec_child_selector',
          required: true,
          label: 'Target item',
          child_types: ['technical_requirement', 'decision'],
        },
      }),
    );

    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-spec')).toBeEnabled(),
    );
    fireEvent.change(screen.getByTestId('discovery-selector-target-spec'), {
      target: { value: 'spec-a' },
    });
    fireEvent.change(screen.getByTestId('discovery-selector-target-child-type'), {
      target: { value: 'technical_requirement' },
    });
    await waitFor(() =>
      expect(discoveryApi.listSelectorOptions).toHaveBeenCalledWith(BOARD, {
        selectorKind: 'spec_child',
        specId: 'spec-a',
        childType: 'technical_requirement',
        q: null,
        status: 'active',
        limit: 50,
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-child')).toBeEnabled(),
    );
    fireEvent.change(screen.getByTestId('discovery-selector-target-child'), {
      target: { value: 'spec:spec-a:technical_requirement:tr-1' },
    });
    fireEvent.click(screen.getByTestId('discovery-params-run'));

    await waitFor(() =>
      expect(discoveryApi.executeIntent).toHaveBeenCalledWith(
        'intent-1',
        BOARD,
        {
          target: {
            spec_id: 'spec-a',
            child_type: 'technical_requirement',
            child_id: 'tr-1',
            child_ref: 'spec:spec-a:technical_requirement:tr-1',
          },
        },
      ),
    );
  });

  it('renders child option labels with the requirement text (subtitle), not just the FR number', async () => {
    mockSelectorOptions(
      selectorResponse([
        {
          id: 'spec:spec-a:functional_requirement:0',
          label: 'FR 1',
          subtitle: 'Analytics coverage rows MUST expose IR/OR…',
          entity_type: 'spec_child',
          spec_id: 'spec-a',
          spec_title: 'Spec A',
          child_type: 'functional_requirement',
          child_id: '0',
          child_ref: 'spec:spec-a:functional_requirement:0',
          status: 'active',
        },
      ]),
    );

    await openParamsForm(
      intent({
        target: {
          type: 'spec_child_selector',
          required: true,
          label: 'Target item',
          child_types: ['functional_requirement'],
        },
      }),
    );

    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-spec')).toBeEnabled(),
    );
    fireEvent.change(screen.getByTestId('discovery-selector-target-spec'), {
      target: { value: 'spec-a' },
    });
    fireEvent.change(screen.getByTestId('discovery-selector-target-child-type'), {
      target: { value: 'functional_requirement' },
    });
    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-child')).toBeEnabled(),
    );
    // "FR 1" sozinho era inutilizável — o texto do requisito acompanha.
    expect(
      screen.getByText('FR 1 — Analytics coverage rows MUST expose IR/OR…'),
    ).toBeInTheDocument();
  });

  it('resets downstream child selections when the parent spec changes', async () => {
    mockSelectorOptions(
      selectorResponse([
        {
          id: 'spec:spec-a:decision:dec-1',
          label: 'DEC-1 Use selectors',
          entity_type: 'spec_child',
          spec_id: 'spec-a',
          child_type: 'decision',
          child_id: 'dec-1',
          child_ref: 'spec:spec-a:decision:dec-1',
        },
      ]),
    );

    await openParamsForm(
      intent({
        target: {
          type: 'spec_child_selector',
          required: true,
          label: 'Target item',
          child_types: ['decision'],
        },
      }),
    );

    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-spec')).toBeEnabled(),
    );
    fireEvent.change(screen.getByTestId('discovery-selector-target-spec'), {
      target: { value: 'spec-a' },
    });
    fireEvent.change(screen.getByTestId('discovery-selector-target-child-type'), {
      target: { value: 'decision' },
    });
    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-child')).toBeEnabled(),
    );
    fireEvent.change(screen.getByTestId('discovery-selector-target-child'), {
      target: { value: 'spec:spec-a:decision:dec-1' },
    });
    expect(screen.getByTestId('discovery-params-run')).toBeEnabled();

    fireEvent.change(screen.getByTestId('discovery-selector-target-spec'), {
      target: { value: 'spec-b' },
    });

    expect(screen.getByTestId('discovery-selector-target-child-type')).toHaveValue('');
    expect(screen.getByTestId('discovery-selector-target-child')).toHaveValue('');
    expect(screen.getByTestId('discovery-params-run')).toBeDisabled();
  });

  it('shows an empty state when a selector endpoint returns zero options', async () => {
    mockSelectorOptions(selectorResponse([]));

    await openParamsForm(
      intent({
        target: {
          type: 'spec_child_selector',
          required: true,
          label: 'Target item',
          child_types: ['api_contract'],
        },
      }),
    );

    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-spec')).toBeEnabled(),
    );
    fireEvent.change(screen.getByTestId('discovery-selector-target-spec'), {
      target: { value: 'spec-a' },
    });
    fireEvent.change(screen.getByTestId('discovery-selector-target-child-type'), {
      target: { value: 'api_contract' },
    });

    expect(
      await screen.findByTestId('discovery-selector-target-child-empty'),
    ).toHaveTextContent('No matching spec children.');
    expect(
      screen.queryByTestId('discovery-selector-target-child-loading'),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('discovery-params-run')).toBeDisabled();
  });

  it('shows a 5xx selector error and retries the failed child option request', async () => {
    let childCalls = 0;
    vi.mocked(discoveryApi.listSelectorOptions).mockImplementation((_board, params) => {
      if (params.selectorKind === 'spec') {
        return Promise.resolve(
          selectorResponse([
            {
              id: 'spec-a',
              label: 'Spec A',
              entity_type: 'spec',
              spec_id: 'spec-a',
            },
          ]),
        );
      }
      childCalls += 1;
      if (childCalls === 1) {
        return Promise.reject(new Error('HTTP 500'));
      }
      return Promise.resolve(
        selectorResponse([
          {
            id: 'spec:spec-a:business_rule:br-1',
            label: 'BR-1 Safe projection',
            entity_type: 'spec_child',
            spec_id: 'spec-a',
            child_type: 'business_rule',
            child_id: 'br-1',
            child_ref: 'spec:spec-a:business_rule:br-1',
          },
        ]),
      );
    });

    await openParamsForm(
      intent({
        target: {
          type: 'spec_child_selector',
          required: true,
          label: 'Target item',
          child_types: ['business_rule'],
        },
      }),
    );

    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-spec')).toBeEnabled(),
    );
    fireEvent.change(screen.getByTestId('discovery-selector-target-spec'), {
      target: { value: 'spec-a' },
    });
    fireEvent.change(screen.getByTestId('discovery-selector-target-child-type'), {
      target: { value: 'business_rule' },
    });

    expect(
      await screen.findByTestId('discovery-selector-target-child-error'),
    ).toHaveTextContent('HTTP 500');
    expect(
      screen.queryByTestId('discovery-selector-target-child-loading'),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() =>
      expect(screen.getByTestId('discovery-selector-target-child')).toBeEnabled(),
    );
    expect(childCalls).toBe(2);
    expect(discoveryApi.listSelectorOptions).toHaveBeenLastCalledWith(BOARD, {
      selectorKind: 'spec_child',
      specId: 'spec-a',
      childType: 'business_rule',
      q: null,
      status: 'active',
      limit: 50,
    });
  });

  it('preserves the existing Open action through parent spec metadata', async () => {
    vi.mocked(discoveryApi.listIntents).mockResolvedValue([intent(null)]);
    vi.mocked(discoveryApi.executeIntent).mockResolvedValue({
      rows: [
        {
          id: 'row-1',
          type: 'technical_requirement',
          title: 'TR-1 Cache invalidation',
          meta: {
            entity_type: 'spec',
            entity_id: 'spec-parent',
            child_type: 'technical_requirement',
            child_ref: 'spec:spec-parent:technical_requirement:tr-1',
          },
        },
      ],
      columns: [],
      total: 1,
      tool_binding: 'okto_pulse_list_uncovered_requirements',
      params_echo: {},
      execution: 'real_tool',
      intent_id: 'intent-1',
      intent_name: 'Trace a spec child',
    });

    render(<GlobalSearchView boardId={BOARD} />);
    fireEvent.click(await screen.findByTestId('discovery-intent-trace_spec_child'));
    fireEvent.click(await screen.findByTestId('discovery-intent-row-0-open'));

    expect(mocks.pushModal).toHaveBeenCalledWith({
      type: 'spec',
      id: 'spec-parent',
    });
  });
});
