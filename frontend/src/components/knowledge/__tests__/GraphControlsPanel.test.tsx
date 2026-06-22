/**
 * Unit coverage for GraphControlsPanel — Spec 8 / Sprint 4 (S4.4, S4.5, S4.6).
 *
 * Verifies the filter wiring: chip toggle emits the correct edgeTypes array,
 * the confidence slider fires with the raw 0..1 value, and the node-limit
 * dropdown bubbles the selected number to `onNodeLimitChange`.
 *
 * These assertions complement the e2e specs (S4.9a / S4.9b) with fast
 * feedback that runs on every CI PR without needing a live dev server.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { GraphControlsPanel } from '../GraphControlsPanel';
import type { Filters } from '../GraphControlsPanel';
import { ALL_EDGE_TYPES, ALL_NODE_TYPES } from '@/types/knowledge-graph';

function baseFilters(overrides: Partial<Filters> = {}): Filters {
  return {
    types: [],
    edgeTypes: [],
    graphLayer: 'canonical',
    minRelevance: 0.5,
    searchQuery: '',
    ...overrides,
  };
}

function renderPanel(props: Partial<React.ComponentProps<typeof GraphControlsPanel>> = {}) {
  const onFiltersChange = vi.fn();
  const onSubViewChange = vi.fn();
  const onNodeLimitChange = vi.fn();
  const utils = render(
    <GraphControlsPanel
      filters={baseFilters()}
      onFiltersChange={onFiltersChange}
      subView="graph"
      onSubViewChange={onSubViewChange}
      nodeCount={42}
      nodeLimit={100}
      onNodeLimitChange={onNodeLimitChange}
      {...props}
    />,
  );
  return { ...utils, onFiltersChange, onSubViewChange, onNodeLimitChange };
}

describe('GraphControlsPanel — edge type chips (S4.4, AC-5)', () => {
  it('renders one chip per KGEdgeType', () => {
    renderPanel();
    for (const et of ALL_EDGE_TYPES) {
      expect(screen.getByTestId(`kg-edge-chip-${et}`)).toBeInTheDocument();
    }
    expect(screen.getAllByTestId(/^kg-edge-chip-/)).toHaveLength(ALL_EDGE_TYPES.length);
  });

  it('includes deterministic lineage and coverage edge types emitted by the backend schema', () => {
    renderPanel();
    expect(screen.getByTestId('kg-edge-chip-originates_from')).toBeInTheDocument();
    expect(screen.getByTestId('kg-edge-chip-covered_by')).toBeInTheDocument();
  });

  it('clicking a chip when all edges are visible hides only that type', () => {
    const { onFiltersChange } = renderPanel();
    fireEvent.click(screen.getByTestId('kg-edge-chip-contradicts'));
    expect(onFiltersChange).toHaveBeenCalledTimes(1);
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.edgeTypes).toHaveLength(ALL_EDGE_TYPES.length - 1);
    expect(next.edgeTypes).not.toContain('contradicts');
  });

  it('clicking an inactive chip re-activates only it', () => {
    const { onFiltersChange } = renderPanel({
      filters: baseFilters({ edgeTypes: ['supersedes'] }),
    });
    fireEvent.click(screen.getByTestId('kg-edge-chip-contradicts'));
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.edgeTypes).toEqual(expect.arrayContaining(['supersedes', 'contradicts']));
    expect(next.edgeTypes).toHaveLength(2);
  });

  it('chips are toggled independently — turning one off leaves others unchanged', () => {
    const { onFiltersChange } = renderPanel({
      filters: baseFilters({ edgeTypes: ['supersedes', 'contradicts', 'tests'] }),
    });
    fireEvent.click(screen.getByTestId('kg-edge-chip-supersedes'));
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.edgeTypes).toEqual(['contradicts', 'tests']);
  });
});

describe('GraphControlsPanel — node type filters', () => {
  it('renders one checkbox per KG node type, including cognitive Learning nodes', () => {
    renderPanel();

    for (const nt of ALL_NODE_TYPES) {
      expect(screen.getByText(new RegExp(`\\b${nt}\\b`))).toBeInTheDocument();
    }
    expect(screen.getByText(/\bLearning\b/)).toBeInTheDocument();
  });

  it('shows zero-count schema node types instead of omitting them from the filter list', () => {
    renderPanel({
      nodeTypeCounts: {
        Decision: 3,
        Learning: 0,
      },
    });

    expect(screen.getByTitle('Total Learning nodes in KG')).toHaveTextContent('0');
    expect(screen.getByText(/\bLearning\b/)).toBeInTheDocument();
  });
});

describe('GraphControlsPanel — graph layer selector', () => {
  it('defaults to the canonical layer and emits working/all changes', () => {
    const { onFiltersChange } = renderPanel();

    expect(screen.getByTestId('kg-graph-layer-canonical')).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    fireEvent.click(screen.getByTestId('kg-graph-layer-working'));
    expect(onFiltersChange).toHaveBeenCalledTimes(1);
    expect((onFiltersChange.mock.calls[0][0] as Filters).graphLayer).toBe('working');
  });

  // R6-TEST3 (ts_ecf530d5): all three layer selectors are exposed and each emits
  // its value coherently — canonical|working|all, never a partial set.
  it('exposes canonical/working/all and emits the selected layer (incl. all)', () => {
    const { onFiltersChange } = renderPanel();
    for (const v of ['canonical', 'working', 'all'] as const) {
      expect(screen.getByTestId(`kg-graph-layer-${v}`)).toBeInTheDocument();
    }
    // canonical is the active default; only working/all are pressable transitions.
    expect(screen.getByTestId('kg-graph-layer-all')).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByTestId('kg-graph-layer-all'));
    expect((onFiltersChange.mock.calls[0][0] as Filters).graphLayer).toBe('all');
  });
});

describe('GraphControlsPanel — relevance slider (S4.5, AC-6)', () => {
  it('slider exposes min=0, max=1, step=0.05 and reflects filter value', () => {
    renderPanel({ filters: baseFilters({ minRelevance: 0.25 }) });
    const slider = screen.getByTestId('kg-relevance-slider') as HTMLInputElement;
    expect(slider.min).toBe('0');
    expect(slider.max).toBe('1');
    expect(slider.step).toBe('0.05');
    expect(slider.value).toBe('0.25');
  });

  it('changing the slider emits the raw 0..1 number, not a percent', () => {
    const { onFiltersChange } = renderPanel();
    fireEvent.change(screen.getByTestId('kg-relevance-slider'), {
      target: { value: '0.75' },
    });
    expect(onFiltersChange).toHaveBeenCalledTimes(1);
    const next = onFiltersChange.mock.calls[0][0] as Filters;
    expect(next.minRelevance).toBeCloseTo(0.75, 5);
  });
});

describe('GraphControlsPanel — node visibility counters', () => {
  it('distinguishes visible, loaded, and total node counts', () => {
    renderPanel({
      visibleNodeCount: 1,
      totalNodeCount: 51,
      nodeTypeCounts: { Decision: 1, Bug: 2 },
    });

    expect(screen.getByText('Node Types (visible 1 / loaded 42 / total 51)')).toBeInTheDocument();
    expect(screen.getByTitle('Total Decision nodes in KG')).toHaveTextContent('1');
    expect(screen.getByTitle('Total Criterion nodes in KG')).toHaveTextContent('0');
  });
});

describe('GraphControlsPanel — node limit dropdown (S4.6, AC-9)', () => {
  it('exposes exactly 50/100/200/500/1000 as options and current value is selected', () => {
    renderPanel({ nodeLimit: 200 });
    const select = screen.getByTestId('kg-node-limit') as HTMLSelectElement;
    const values = [...select.options].map((o) => Number(o.value));
    expect(values).toEqual([50, 100, 200, 500, 1000]);
    expect(select.value).toBe('200');
  });

  it('selecting a new option fires onNodeLimitChange with the chosen number', () => {
    const { onNodeLimitChange } = renderPanel();
    fireEvent.change(screen.getByTestId('kg-node-limit'), { target: { value: '1000' } });
    expect(onNodeLimitChange).toHaveBeenCalledWith(1000);
  });
});
